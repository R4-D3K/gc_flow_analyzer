"""
Flow execution data parser.

Transforms the raw Genesys Cloud execution data JSON into clean,
structured Python objects that the templates can render easily.
Also generates Mermaid diagram source for visual flow representation.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import re


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FlowStep:
    sequence: int
    action_id: str
    action_name: str
    action_type: str
    flow_name: str          # which flow/subflow this step belongs to
    start_time: str
    end_time: str
    duration_ms: Optional[int]
    inputs: dict
    outputs: dict
    error: Optional[str]
    variables_after: dict   # snapshot of relevant variables after this step


@dataclass
class ParsedFlowInstance:
    instance_id: str
    flow_id: str
    flow_name: str
    flow_type: str
    flow_version: str
    start_time: str
    end_time: str
    exit_reason: str
    total_steps: int
    steps: list[FlowStep]
    variable_timeline: list[dict]   # [{step_seq, name, old_val, new_val}]
    mermaid_diagram: str


@dataclass
class ParsedConversation:
    conversation_id: str
    ani: str
    called_address: str
    language: str
    instances: list[ParsedFlowInstance]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_str(val) -> str:
    if val is None:
        return ""
    return str(val)


def _duration_ms(start: str, end: str) -> Optional[int]:
    """Calculate duration in ms between two ISO-8601 timestamps."""
    if not start or not end:
        return None
    try:
        from datetime import datetime, timezone
        fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
        def parse(s):
            s = s.replace("+00:00", "Z")
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                return datetime.fromisoformat(s.rstrip("Z"))
        return int((parse(end) - parse(start)).total_seconds() * 1000)
    except Exception:
        return None


def _extract_conv_variables(conv_details: dict) -> dict:
    """Extract key call variables from conversation details for display."""
    result = {}
    for p in conv_details.get("participants", []):
        for s in p.get("sessions", []):
            for attr_name in ("ani", "dnis", "direction"):
                val = s.get(attr_name)
                if val:
                    result[attr_name.upper()] = val
    return result


def _flatten_execution_items(execution_data: dict) -> list[dict]:
    """
    Extract execution items list from the job result dict.
    GC returns them nested under executionData.execution.executionItems
    or directly under execution.executionItems depending on expand level.
    """
    # Try multiple known paths
    candidates = [
        execution_data.get("execution_data", {}) or {},
        execution_data.get("executionData", {}) or {},
        execution_data,
    ]
    for candidate in candidates:
        items = (
            candidate.get("execution", {}).get("executionItems")
            or candidate.get("executionItems")
            or candidate.get("execution_items")
        )
        if items:
            return items
    return []


def _normalize_step(seq: int, raw: dict, flow_name: str) -> FlowStep:
    """Convert a raw execution item dict into a FlowStep."""

    def get(key, alt=None):
        # Support both camelCase and snake_case keys
        return raw.get(key) or raw.get(_to_snake(key)) or alt

    action_id = _safe_str(get("actionId", get("id", "")))
    action_name = _safe_str(get("actionName", get("name", f"Step {seq}")))
    action_type = _safe_str(get("actionType", get("type", "")))
    start_time = _safe_str(get("startDateTime", get("startTime", "")))
    end_time = _safe_str(get("endDateTime", get("endTime", "")))

    inputs = get("inputs", {}) or {}
    outputs = get("outputs", {}) or {}
    error_info = get("error") or get("errorInfo")
    error_msg = None
    if error_info:
        error_msg = (
            error_info.get("message")
            or error_info.get("errorMessage")
            or str(error_info)
        )

    # Variables snapshot: merge inputs + outputs as post-step state
    variables_after = {}
    if isinstance(inputs, dict):
        variables_after.update({f"in.{k}": v for k, v in inputs.items()})
    if isinstance(outputs, dict):
        variables_after.update({f"out.{k}": v for k, v in outputs.items()})

    return FlowStep(
        sequence=seq,
        action_id=action_id,
        action_name=action_name,
        action_type=action_type,
        flow_name=flow_name,
        start_time=start_time,
        end_time=end_time,
        duration_ms=_duration_ms(start_time, end_time),
        inputs=inputs if isinstance(inputs, dict) else {},
        outputs=outputs if isinstance(outputs, dict) else {},
        error=error_msg,
        variables_after=variables_after,
    )


def _to_snake(name: str) -> str:
    """Convert camelCase to snake_case."""
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


# ---------------------------------------------------------------------------
# Mermaid diagram generation
# ---------------------------------------------------------------------------

_ACTION_TYPE_STYLES = {
    # Decision/branch nodes
    "decision": "diamond",
    "menu": "diamond",
    "switch": "diamond",
    # Task/subflow nodes
    "calldata": "round",
    "task": "round",
    "calltask": "round",
    # Transfer/end nodes
    "transfer": "trapezoid",
    "disconnect": "trapezoid",
    "end": "trapezoid",
    # Data action
    "dataaction": "stadium",
    # Default
    "default": "rect",
}

_MERMAID_SHAPES = {
    "diamond": ("{", "}"),
    "round": ("(", ")"),
    "trapezoid": ("[/", "\\]"),
    "stadium": ("([", "])"),
    "rect": ("[", "]"),
}


def _sanitize_label(text: str) -> str:
    """Make text safe for Mermaid labels."""
    return text.replace('"', "'").replace("\n", " ")[:60]


def _mermaid_node(step: FlowStep) -> str:
    node_id = f"S{step.sequence}"
    shape_key = _ACTION_TYPE_STYLES.get(step.action_type.lower(), "default")
    open_b, close_b = _MERMAID_SHAPES[shape_key]
    label = _sanitize_label(f"{step.sequence}. {step.action_name}")

    if step.error:
        return f'    {node_id}{open_b}"{label}"{close_b}\n    style {node_id} fill:#ff6b6b,color:#fff'
    return f'    {node_id}{open_b}"{label}"{close_b}'


def generate_mermaid(steps: list[FlowStep]) -> str:
    """Generate a Mermaid flowchart from the list of steps."""
    if not steps:
        return "flowchart TD\n    A[No execution steps found]"

    lines = ["flowchart TD"]

    # Limit to first 80 steps to keep diagram readable
    display_steps = steps[:80]
    truncated = len(steps) > 80

    # Node definitions
    for step in display_steps:
        lines.append(_mermaid_node(step))

    # Edges
    for i in range(len(display_steps) - 1):
        curr = display_steps[i]
        nxt = display_steps[i + 1]
        lines.append(f"    S{curr.sequence} --> S{nxt.sequence}")

    if truncated:
        lines.append(f'    S{display_steps[-1].sequence} --> TRUNC["... {len(steps) - 80} more steps"]')
        lines.append('    style TRUNC fill:#aaa,color:#fff')

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Variable change timeline
# ---------------------------------------------------------------------------

def _build_variable_timeline(steps: list[FlowStep]) -> list[dict]:
    """
    Track variable changes across steps.
    Returns list of {step_seq, step_name, name, old_val, new_val}
    """
    timeline = []
    current_vars: dict = {}

    for step in steps:
        # Only track output variables as they represent state changes
        outputs = step.outputs
        if not isinstance(outputs, dict):
            continue
        for var_name, new_val in outputs.items():
            old_val = current_vars.get(var_name, "NOT_SET")
            new_val_str = _safe_str(new_val)
            if old_val != new_val_str:
                timeline.append({
                    "step_seq": step.sequence,
                    "step_name": step.action_name,
                    "name": var_name,
                    "old_val": old_val,
                    "new_val": new_val_str,
                })
                current_vars[var_name] = new_val_str

    return timeline


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def parse_execution_data(raw_data: dict) -> ParsedConversation:
    """
    Transform the raw output of gc_client.get_flow_execution_data()
    into a ParsedConversation ready for template rendering.
    """
    conversation_id = raw_data.get("conversationId", "")
    conv_details = raw_data.get("conversationDetails", {})

    # Extract simple call info from conversation details
    conv_vars = _extract_conv_variables(conv_details)
    ani = conv_vars.get("ANI", "")
    called = conv_vars.get("DNIS", "")
    language = ""
    # Try to find language in participants
    for p in conv_details.get("participants", []):
        for s in p.get("sessions", []):
            lang = s.get("media_type") and None or s.get("language") or s.get("ani_normalized")
            # language is often in flow variables, get from session attributes
            attrs = s.get("attributes") or {}
            if attrs.get("language"):
                language = attrs["language"]
                break

    parsed_instances = []

    for flow_inst in raw_data.get("flowInstances", []):
        meta = flow_inst.get("instanceMeta", {})
        exec_data = flow_inst.get("executionData", {})

        flow_name = meta.get("flowName", "Unknown Flow")
        instance_id = meta.get("flowInstanceId", "")

        raw_items = _flatten_execution_items(exec_data)

        steps = []
        for seq, raw_item in enumerate(raw_items, start=1):
            step = _normalize_step(seq, raw_item, flow_name)
            steps.append(step)

        variable_timeline = _build_variable_timeline(steps)
        mermaid_src = generate_mermaid(steps)

        parsed_instances.append(ParsedFlowInstance(
            instance_id=instance_id,
            flow_id=meta.get("flowId", ""),
            flow_name=flow_name,
            flow_type=meta.get("flowType", ""),
            flow_version=meta.get("flowVersion", ""),
            start_time=meta.get("startTime", ""),
            end_time=meta.get("endTime", ""),
            exit_reason=meta.get("exitReason", ""),
            total_steps=len(steps),
            steps=steps,
            variable_timeline=variable_timeline,
            mermaid_diagram=mermaid_src,
        ))

    return ParsedConversation(
        conversation_id=conversation_id,
        ani=ani,
        called_address=called,
        language=language,
        instances=parsed_instances,
    )
