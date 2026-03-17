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
import logging

logger = logging.getLogger(__name__)


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
    depth: int = 0          # nesting depth (0 = top-level, 1 = inside task, etc.)
    parent_task: str = ""   # name of the containing task (if depth > 0)


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


def _get_raw_execution_items(execution_data: dict) -> list:
    """Extract the top-level execution items list from the downloaded JSON."""
    flow_node = execution_data.get("flow")
    if isinstance(flow_node, dict):
        exec_node = flow_node.get("execution")
        if isinstance(exec_node, list):
            return exec_node
        if isinstance(exec_node, dict):
            for key in ("executionItems", "execution_items", "items", "actions", "steps"):
                items = exec_node.get(key)
                if items and isinstance(items, list):
                    return items

    # Fallback: older patterns
    for candidate in [
        execution_data.get("execution_data") or {},
        execution_data.get("executionData") or {},
        execution_data,
    ]:
        if not isinstance(candidate, dict):
            continue
        exec_node = candidate.get("execution") or {}
        for key in ("executionItems", "execution_items", "items", "actions", "steps"):
            items = exec_node.get(key) if isinstance(exec_node, dict) else None
            if items and isinstance(items, list):
                return items
        for key in ("executionItems", "execution_items", "items"):
            items = candidate.get(key)
            if items and isinstance(items, list):
                return items
    return []


def _expand_items_recursive(items: list, depth: int, parent_task: str) -> list[dict]:
    """
    Recursively expand execution items, inlining nested sub-task executions.
    Items inside actionCallTask.execution are annotated with _depth and _parentTask
    so _normalize_step can display them indented.
    """
    result = []
    for item in items:
        if not isinstance(item, dict) or not item:
            continue

        event_type = list(item.keys())[0]
        ed = item[event_type]

        # Annotate non-top-level items with parent context
        if depth > 0 and isinstance(ed, dict):
            annotated_ed = dict(ed)
            annotated_ed["_depth"] = depth
            annotated_ed["_parentTask"] = parent_task
            result.append({event_type: annotated_ed})
        else:
            result.append(item)

        # Inline nested execution for call-task events
        if event_type in ("actionCallTask", "actionCallModule") and isinstance(ed, dict):
            nested = ed.get("execution")
            if isinstance(nested, list) and nested:
                task_name = ed.get("actionName", "")
                result.extend(_expand_items_recursive(nested, depth + 1, task_name))

    return result


def _flatten_execution_items(execution_data: dict) -> list[dict]:
    """
    Extract and expand execution items from downloaded GC renderedData JSON.
    Recursively inlines nested sub-task executions (actionCallTask.execution)
    so data actions and other nested steps appear in the flat step list.
    """
    raw = _get_raw_execution_items(execution_data)
    return _expand_items_recursive(raw, depth=0, parent_task="")


def _vars_list_to_dict(lst: list) -> dict:
    """Convert [{variableName/name/outputName, value}] to {name: value}."""
    result = {}
    for item in lst:
        if not isinstance(item, dict):
            continue
        name = (item.get("variableName") or item.get("outputName")
                or item.get("name", ""))
        if name:
            result[name] = item.get("value")
    return result


def _normalize_step(seq: int, raw: dict, flow_name: str) -> FlowStep:
    """
    Convert a GC renderedData execution item into a FlowStep.
    Each item has exactly one key = event type, value = event data dict.
    """
    if not isinstance(raw, dict) or not raw:
        return FlowStep(
            sequence=seq, action_id="", action_name=f"Step {seq}",
            action_type="unknown", flow_name=flow_name, start_time="",
            end_time="", duration_ms=None, inputs={}, outputs={},
            error=None, variables_after={},
        )

    event_type = list(raw.keys())[0]
    ed = raw[event_type] if isinstance(raw[event_type], dict) else {}

    action_id   = _safe_str(ed.get("actionId", ""))
    action_name = _safe_str(
        ed.get("actionName") or ed.get("taskName") or event_type
    )
    action_type = event_type
    start_time  = _safe_str(ed.get("dateTime", ""))
    # Nesting metadata injected by _expand_items_recursive
    depth       = int(ed.get("_depth", 0))
    parent_task = _safe_str(ed.get("_parentTask", ""))

    # End time: for events with nested sub-execution use last nested dateTime
    end_time = ""
    nested = ed.get("execution")
    if isinstance(nested, list) and nested:
        for ne in reversed(nested):
            if isinstance(ne, dict) and ne:
                ne_data = next(iter(ne.values()), {})
                if isinstance(ne_data, dict) and ne_data.get("dateTime"):
                    end_time = ne_data["dateTime"]
                    break

    inputs:  dict = {}
    outputs: dict = {}

    # --- startedFlow / startedTask: initial variable snapshot ---
    if event_type in ("startedFlow", "startedTask"):
        vars_list = ed.get("variables") or []
        if isinstance(vars_list, list):
            inputs = _vars_list_to_dict(vars_list)

    # --- actionUpdateData: variable assignments via statements ---
    elif event_type == "actionUpdateData":
        outputs = _vars_list_to_dict(ed.get("statements") or [])

    # --- actionGetParticipantData: attributes → outputs + outputVariables for flow vars ---
    elif event_type == "actionGetParticipantData":
        # Display: participant attribute values (outputName → value)
        attr_list = (ed.get("outputData") or {}).get("attributes") or []
        outputs = _vars_list_to_dict(attr_list)
        # Flow variable assignments (richer — override attr names if present)
        ov = _vars_list_to_dict(ed.get("outputVariables") or [])
        outputs.update(ov)

    # --- actionCallTask / actionCallModule: task I/O ---
    elif event_type in ("actionCallTask", "actionCallModule"):
        # inputs: non-empty task input parameters
        task_inputs = (ed.get("inputData") or {}).get("taskInputs") or []
        inputs = _vars_list_to_dict(task_inputs) if task_inputs else {}
        # outputs: outputVariables (preferred) or taskOutputs
        ov = _vars_list_to_dict(ed.get("outputVariables") or [])
        if ov:
            outputs = ov
        else:
            task_outputs = (ed.get("outputData") or {}).get("taskOutputs") or []
            outputs = _vars_list_to_dict(task_outputs) if task_outputs else {}
        # annotate target task name
        target = ed.get("targetTask") or {}
        if target.get("taskName"):
            inputs["_targetTask"] = target["taskName"]

    # --- actionSwitch / actionDecision: show cases + selected path ---
    elif event_type in ("actionSwitch", "actionDecision", "actionMenu"):
        cases = (ed.get("inputData") or {}).get("cases") or []
        for case in cases:
            if isinstance(case, dict):
                inputs[case.get("inputName", "case")] = case.get("value")
        if ed.get("outputPathName"):
            outputs["_selectedPath"] = ed["outputPathName"]
        elif ed.get("outputPathId"):
            outputs["_selectedPathId"] = ed["outputPathId"]

    # --- actionSetParticipantData: show what's being set (inputName → value) ---
    elif event_type == "actionSetParticipantData":
        attr_list = (ed.get("inputData") or {}).get("attributes") or []
        for attr in attr_list:
            if isinstance(attr, dict):
                name = attr.get("inputName") or attr.get("name", "")
                if name:
                    inputs[name] = attr.get("value")

    # --- endedFlow: exit reason ---
    elif event_type == "endedFlow":
        if ed.get("flowExitReason"):
            outputs["flowExitReason"] = ed["flowExitReason"]
        outputs.update(_vars_list_to_dict(ed.get("outputVariables") or []))

    # --- endedTask ---
    elif event_type == "endedTask":
        outputs = _vars_list_to_dict(ed.get("outputVariables") or [])

    # --- actionCallData: data action call with request/response ---
    elif event_type == "actionCallData":
        # inputData: request parameters sent to the data action
        input_data = ed.get("inputData")
        if isinstance(input_data, dict):
            for k, v in input_data.items():
                if not k.startswith("_"):
                    inputs[k] = v if not isinstance(v, (dict, list)) else str(v)
        # outputVariables: flow variables assigned from the response (most useful)
        ov = _vars_list_to_dict(ed.get("outputVariables") or [])
        if ov:
            outputs = ov
        else:
            # Fall back to raw outputData if no variable mappings
            output_data = ed.get("outputData")
            if isinstance(output_data, dict):
                for k, v in output_data.items():
                    if not k.startswith("_"):
                        outputs[k] = v if not isinstance(v, (dict, list)) else str(v)

    # --- actionDataTableLookup: lookup inputs + matched row outputs ---
    elif event_type == "actionDataTableLookup":
        input_data = ed.get("inputData")
        if isinstance(input_data, dict):
            for k, v in input_data.items():
                if not k.startswith("_"):
                    inputs[k] = v if not isinstance(v, (dict, list)) else str(v)
        ov = _vars_list_to_dict(ed.get("outputVariables") or [])
        if ov:
            outputs = ov
        else:
            output_data = ed.get("outputData")
            if isinstance(output_data, dict):
                for k, v in output_data.items():
                    if not k.startswith("_"):
                        outputs[k] = v if not isinstance(v, (dict, list)) else str(v)

    # --- generic fallback for other action types ---
    else:
        input_data = ed.get("inputData")
        if isinstance(input_data, dict):
            inputs = input_data
        output_data = ed.get("outputData")
        if isinstance(output_data, dict):
            outputs.update(output_data)
        outputs.update(_vars_list_to_dict(ed.get("outputVariables") or []))
        outputs.update(_vars_list_to_dict(ed.get("statements") or []))
        if ed.get("outputPathName"):
            outputs["_selectedPath"] = ed["outputPathName"]

    # Error
    error_msg = None
    error_info = ed.get("error") or ed.get("errorInfo")
    if error_info:
        error_msg = (
            error_info.get("message") or error_info.get("errorMessage")
            or str(error_info)
        ) if isinstance(error_info, dict) else str(error_info)

    variables_after = dict(inputs)
    variables_after.update(outputs)

    return FlowStep(
        sequence=seq,
        action_id=action_id,
        action_name=action_name,
        action_type=action_type,
        flow_name=flow_name,
        start_time=start_time,
        end_time=end_time,
        duration_ms=_duration_ms(start_time, end_time),
        inputs=inputs,
        outputs=outputs,
        error=error_msg,
        variables_after=variables_after,
        depth=depth,
        parent_task=parent_task,
    )


def _to_snake(name: str) -> str:
    """Convert camelCase to snake_case."""
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


# ---------------------------------------------------------------------------
# Mermaid diagram generation
# ---------------------------------------------------------------------------

_ACTION_TYPE_STYLES = {
    # Decision/branch — renderedData event names
    "actionswitch": "diamond",
    "actiondecision": "diamond",
    "actionmenu": "diamond",
    # Subflow/task call
    "actioncalltask": "round",
    "actioncallmodule": "round",
    "actioncalllexbot": "round",
    "actioncalldialogflowbot": "round",
    # Transfer / end
    "actiontransfer": "trapezoid",
    "actiontransfertousers": "trapezoid",
    "actiontransfertoqueue": "trapezoid",
    "actiontransfertovoicemail": "trapezoid",
    "actiondisconnect": "trapezoid",
    "endedflow": "trapezoid",
    # Data actions
    "actioncalldata": "stadium",
    "actiondataaction": "stadium",
    "actiongetdata": "stadium",
    "actiondatatableloookup": "stadium",
    "actioninvokestate": "stadium",
    # Flow/task lifecycle
    "startedflow": "round",
    "startedtask": "round",
    "endedtask": "round",
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
    shape_key = _ACTION_TYPE_STYLES.get(step.action_type.lower(), "rect")
    open_b, close_b = _MERMAID_SHAPES[shape_key]
    label = _sanitize_label(f"{step.sequence}. {step.action_name}")

    if step.error:
        return f'    {node_id}{open_b}"{label}"{close_b}\n    style {node_id} fill:#ff6b6b,color:#fff'
    return f'    {node_id}{open_b}"{label}"{close_b}'


def generate_mermaid(steps: list[FlowStep]) -> str:
    """Generate a Mermaid flowchart from top-level steps only."""
    top_level = [s for s in steps if s.depth == 0]
    if not top_level:
        return "flowchart TD\n    A[No execution steps found]"

    lines = ["flowchart LR"]

    # Limit to first 80 top-level steps to keep diagram readable
    display_steps = top_level[:80]
    truncated = len(top_level) > 80

    # Node definitions
    for step in display_steps:
        lines.append(_mermaid_node(step))

    # Edges
    for i in range(len(display_steps) - 1):
        curr = display_steps[i]
        nxt = display_steps[i + 1]
        lines.append(f"    S{curr.sequence} --> S{nxt.sequence}")

    if truncated:
        lines.append(f'    S{display_steps[-1].sequence} --> TRUNC["... {len(top_level) - 80} more steps"]')
        lines.append('    style TRUNC fill:#aaa,color:#fff')

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Variable change timeline
# ---------------------------------------------------------------------------

def _build_variable_timeline(steps: list[FlowStep]) -> list[dict]:
    """
    Track variable changes across steps.
    Scans both inputs (for startedFlow/startedTask initial values) and outputs.
    Skips internal keys (prefixed _) and empty names.
    Returns list of {step_seq, step_name, action_type, name, old_val, new_val}
    """
    timeline = []
    current_vars: dict = {}

    for step in steps:
        # For lifecycle events track inputs (initial variable snapshot)
        if step.action_type in ("startedFlow", "startedTask"):
            candidates = step.inputs
        else:
            candidates = step.outputs

        if not isinstance(candidates, dict):
            continue

        for var_name, new_val in candidates.items():
            # Skip internal/structural keys
            if not var_name or var_name.startswith("_"):
                continue
            # Skip complex/empty values that aren't meaningful variable states
            if isinstance(new_val, list) and not new_val:
                continue
            if isinstance(new_val, dict) and not new_val:
                continue
            old_val = current_vars.get(var_name, "NOT_SET")
            new_val_str = _safe_str(new_val)
            if old_val != new_val_str:
                timeline.append({
                    "step_seq": step.sequence,
                    "step_name": step.action_name,
                    "action_type": step.action_type,
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
        logger.info("parse: %d raw execution items for flow '%s'", len(raw_items), flow_name)
        if raw_items:
            logger.info("  first item keys: %s", list(raw_items[0].keys()) if isinstance(raw_items[0], dict) else type(raw_items[0]).__name__)
            logger.info("  first item sample: %s", str(raw_items[0])[:300])

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
