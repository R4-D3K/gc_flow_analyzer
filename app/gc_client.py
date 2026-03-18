"""
Genesys Cloud API client wrapper.

Handles authentication (Client Credentials) and all API calls needed
to retrieve flow execution data for a given ConversationId.

Supports multi-org mode: each org has its own cached ApiClient keyed by client_id.
Falls back to single-org config (GC_CLIENT_ID / GC_CLIENT_SECRET / GC_ENVIRONMENT)
when org=None is passed.
"""

import time
import logging
from typing import Optional

import httpx
import PureCloudPlatformClientV2 as gc
from PureCloudPlatformClientV2.rest import ApiException

from app import config

logger = logging.getLogger(__name__)

# How long (seconds) to wait between job status polls
_POLL_INTERVAL = 2
# Maximum number of poll attempts before giving up (~60 seconds total)
_POLL_MAX_ATTEMPTS = 30

# Token cache: dict keyed by client_id → (ApiClient, expires_at)
_api_client_cache: dict[str, tuple[gc.ApiClient, float]] = {}

# Refresh 60 s before actual expiry to avoid edge-case 401s
_TOKEN_EXPIRY_SECONDS = 3600
_TOKEN_REFRESH_BUFFER = 60


class GCClientError(Exception):
    """Raised when a Genesys Cloud API call fails."""


def _resolve_org_dict(org: dict | None) -> dict:
    """
    Return a normalised org dict. If org is None, build one from single-org config.
    """
    if org is not None:
        return org
    return {
        "name":          "Default",
        "environment":   config.GC_ENVIRONMENT,
        "client_id":     config.GC_CLIENT_ID,
        "client_secret": config.GC_CLIENT_SECRET,
    }


def _get_api_client(org: dict) -> gc.ApiClient:
    """
    Return an authenticated ApiClient for the given org, reusing a cached one
    if the token is still valid.  GC Client Credentials tokens are valid for ~1 hour.
    """
    global _api_client_cache

    client_id = org["client_id"]
    now = time.monotonic()

    cached = _api_client_cache.get(client_id)
    if cached is not None:
        api_client, expires_at = cached
        if now < expires_at:
            logger.debug(
                "Reusing cached GC token for org '%s' (expires in %.0fs)",
                org.get("name", client_id[:8]),
                expires_at - now,
            )
            return api_client

    logger.info("Obtaining new GC access token for org '%s'", org.get("name", client_id[:8]))
    gc.configuration.host = f"https://api.{org['environment']}"
    api_client = gc.api_client.ApiClient()

    try:
        token_response = api_client.get_client_credentials_token(
            org["client_id"], org["client_secret"]
        )
        api_client.set_default_header("Authorization", f"Bearer {token_response.access_token}")
        _api_client_cache[client_id] = (
            api_client,
            now + _TOKEN_EXPIRY_SECONDS - _TOKEN_REFRESH_BUFFER,
        )
        logger.info(
            "Genesys Cloud authentication successful for org '%s', token cached for %ds",
            org.get("name", client_id[:8]),
            _TOKEN_EXPIRY_SECONDS - _TOKEN_REFRESH_BUFFER,
        )
        return api_client
    except ApiException as e:
        raise GCClientError(f"Authentication failed: {e.status} {e.reason}") from e


def get_conversation_details(conversation_id: str, org: dict) -> dict:
    """
    Fetch conversation details from Analytics API.
    Returns the raw dict from AnalyticsConversationWithoutAttributes.
    """
    api_client = _get_api_client(org)
    analytics_api = gc.ConversationsApi(api_client)

    try:
        result = analytics_api.get_analytics_conversation_details(conversation_id)
        return result.to_dict()
    except ApiException as e:
        if e.status == 404:
            raise GCClientError(f"Conversation '{conversation_id}' not found.") from e
        raise GCClientError(f"Failed to fetch conversation details: {e.status} {e.reason}") from e


def _extract_flow_instance_ids(conversation_details: dict) -> list[dict]:
    """
    Walk through conversation participants/sessions to collect all flow instance runs.
    Returns list of dicts: {flowInstanceId, flowName, flowType, startTime, endTime}
    """
    logger.info("conversation_details top-level keys: %s", list(conversation_details.keys()))
    participants = conversation_details.get("participants", [])
    logger.info("_extract_flow_instance_ids: %d participant(s)", len(participants))

    instances = []
    for p_idx, participant in enumerate(participants):
        sessions = participant.get("sessions", [])
        logger.info("  participant[%d] purpose=%s sessions=%d",
                    p_idx, participant.get("purpose", "?"), len(sessions))
        for s_idx, session in enumerate(sessions):
            # SDK v233 uses singular 'flow' (dict), older versions used 'flows' (list)
            flow_singular = session.get("flow")
            flows_plural = session.get("flows") or []
            # Normalise to a list of flow dicts
            if flow_singular and isinstance(flow_singular, dict):
                flows_raw = [flow_singular]
            elif flows_plural:
                flows_raw = flows_plural if isinstance(flows_plural, list) else [flows_plural]
            else:
                flows_raw = []
            logger.info("    session[%d] flow(singular)=%s flows(list)=%d → using %d",
                        s_idx, bool(flow_singular), len(flows_plural), len(flows_raw))
            for f_idx, flow in enumerate(flows_raw):
                logger.info("      flow[%d] keys=%s", f_idx, list(flow.keys()))
                instance_id = (
                    flow.get("flow_instance_id")
                    or flow.get("flowInstanceId")
                )
                if not instance_id:
                    logger.warning(
                        "      flow[%d] has no flow_instance_id — "
                        "Execution Data logging is likely disabled in Architect for flow '%s'",
                        f_idx, flow.get("flow_name", "?")
                    )
                if instance_id and instance_id not in [i["flowInstanceId"] for i in instances]:
                    instances.append({
                        "flowInstanceId": instance_id,
                        "flowId": flow.get("flow_id") or flow.get("flowId", ""),
                        "flowName": flow.get("flow_name") or flow.get("flowName", ""),
                        "flowType": flow.get("flow_type") or flow.get("flowType", ""),
                        "flowVersion": flow.get("flow_version") or flow.get("flowVersion", ""),
                        "startTime": flow.get("flow_start_timestamp") or flow.get("startTime", ""),
                        "endTime": flow.get("flow_end_timestamp") or flow.get("endTime", ""),
                        "exitReason": flow.get("exit_reason") or flow.get("exitReason", ""),
                    })

    logger.info("_extract_flow_instance_ids: found %d instance(s)", len(instances))
    return instances


def _lookup_instances_by_conversation(api_client: gc.ApiClient, conversation_id: str) -> list[dict]:
    """
    Fallback: query POST /api/v2/flows/instances/query by conversationId.
    Used when analytics conversation details don't include flow_instance_id.
    Returns FlowExecutionDataQueryResult entities with id = flowInstanceId.
    """
    flow_api = gc.ArchitectApi(api_client)
    try:
        item = gc.CriteriaItem()
        item.key = "ConversationId"
        item.operator = "eq"
        item.value = conversation_id

        group = gc.CriteriaGroup()
        group.criteria = item

        query = gc.CriteriaQuery()
        query.query = [group]

        result = flow_api.post_flows_instances_query(query, page_size=25)
        result_dict = result.to_dict()
        entities = result_dict.get("entities") or []
        logger.info("post_flows_instances_query fallback: %d entity(ies)", len(entities))

        instances = []
        for ent in entities:
            logger.info("  entity keys=%s", list(ent.keys()))
            instance_id = ent.get("id")
            if instance_id and instance_id not in [i["flowInstanceId"] for i in instances]:
                instances.append({
                    "flowInstanceId": instance_id,
                    "flowId": ent.get("flow_id", ""),
                    "flowName": ent.get("flow_name", ""),
                    "flowType": ent.get("flow_type", ""),
                    "flowVersion": ent.get("flow_version", ""),
                    "startTime": _safe_dt(ent.get("start_date_time")),
                    "endTime": _safe_dt(ent.get("end_date_time")),
                    "exitReason": ent.get("flow_error_reason") or ent.get("flow_warning_reason", ""),
                })
        return instances
    except ApiException as e:
        logger.warning("post_flows_instances_query fallback failed: %s %s", e.status, e.reason)
        return []


def _safe_dt(val) -> str:
    """Convert datetime object or string to ISO string, or return ''."""
    if val is None:
        return ""
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


def _start_execution_data_job(api_client: gc.ApiClient, instance_id: str) -> tuple[str, dict | None]:
    """
    Start (or resume) an async job to prepare execution data for a flow instance.
    Returns (job_id, execution_data_or_None).
    If the job is already fulfilled on first call, returns data immediately.
    """
    flow_api = gc.ArchitectApi(api_client)
    try:
        result = flow_api.get_flows_instance(instance_id, expand="execution")
        result_dict = result.to_dict()
        job_id = result_dict.get("id")
        job_state = (result_dict.get("job_state") or "").upper()
        entities = result_dict.get("entities") or []
        logger.info(
            "get_flows_instance: job_id=%s job_state=%s entities=%d",
            job_id, job_state, len(entities)
        )
        if not job_id:
            raise GCClientError("No job id returned when starting execution data job.")

        # If already fulfilled on first call, download immediately
        if job_state in ("FULFILLED", "SUCCESS") and entities:
            entity = entities[0]
            if entity.get("download_uri"):
                logger.info("Job already fulfilled, downloading immediately")
                return job_id, _download_execution_data(entity["download_uri"])

        return job_id, None
    except ApiException as e:
        raise GCClientError(
            f"Failed to start execution job for instance '{instance_id}': {e.status} {e.reason}"
        ) from e


def _download_execution_data(download_uri: str) -> dict:
    """Download execution data JSON from the pre-signed URI returned by the job."""
    logger.info("Downloading execution data from URI: %s", download_uri[:80])
    try:
        response = httpx.get(download_uri, timeout=30.0, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            logger.info("Execution data downloaded, top-level keys: %s", list(data.keys()))
            for k, v in data.items():
                if isinstance(v, dict):
                    logger.info("  [%s] → dict keys: %s", k, list(v.keys()))
                elif isinstance(v, list):
                    logger.info("  [%s] → list len=%d, first item keys: %s", k, len(v),
                                list(v[0].keys()) if v and isinstance(v[0], dict) else (v[0] if v else "empty"))
                else:
                    logger.info("  [%s] → %s: %s", k, type(v).__name__, str(v)[:80])
            # Log one level deeper for 'flow.execution'
            flow_node = data.get("flow", {})
            execution_node = flow_node.get("execution") if isinstance(flow_node, dict) else None
            if isinstance(execution_node, dict):
                logger.info("  flow.execution → dict, keys: %s", list(execution_node.keys()))
                for ek, ev in execution_node.items():
                    if isinstance(ev, list):
                        logger.info("    flow.execution[%s] → list len=%d, first item keys: %s",
                                    ek, len(ev),
                                    list(ev[0].keys()) if ev and isinstance(ev[0], dict) else (ev[0] if ev else "empty"))
                    elif isinstance(ev, dict):
                        logger.info("    flow.execution[%s] → dict keys: %s", ek, list(ev.keys()))
                    else:
                        logger.info("    flow.execution[%s] → %s: %s", ek, type(ev).__name__, str(ev)[:60])
            elif isinstance(execution_node, list):
                logger.info("  flow.execution → list len=%d, first item keys: %s",
                            len(execution_node),
                            list(execution_node[0].keys()) if execution_node and isinstance(execution_node[0], dict) else "")
            else:
                logger.info("  flow.execution → %s", type(execution_node).__name__)
        elif isinstance(data, list):
            logger.info("Execution data is a list, len=%d, first item keys: %s", len(data),
                        list(data[0].keys()) if data and isinstance(data[0], dict) else "")
        return data
    except Exception as e:
        raise GCClientError(f"Failed to download execution data: {e}") from e


def _poll_job(api_client: gc.ApiClient, job_id: str) -> dict:
    """
    Poll the job endpoint until it completes.
    Returns the downloaded execution data JSON dict.
    GetFlowExecutionDataJobResult uses 'job_state' (not 'status') and
    delivers a download_uri in entities[0] when fulfilled.
    """
    flow_api = gc.ArchitectApi(api_client)
    for attempt in range(_POLL_MAX_ATTEMPTS):
        try:
            result = flow_api.get_flows_instances_job(job_id)
            result_dict = result.to_dict()
            job_state = result_dict.get("job_state", "").upper()
            logger.info("Job %s state: %s (attempt %d)", job_id, job_state, attempt + 1)

            if job_state in ("FULFILLED", "SUCCESS"):
                entities = result_dict.get("entities") or []
                if not entities:
                    raise GCClientError("Job fulfilled but no entities in result.")
                entity = entities[0]
                logger.info("Job fulfilled, entity keys: %s", list(entity.keys()))
                if entity.get("failed"):
                    raise GCClientError(
                        f"Execution data entity failed: {entity.get('status_code', 'unknown')}"
                    )
                download_uri = entity.get("download_uri")
                if not download_uri:
                    raise GCClientError("Job fulfilled but entity has no download_uri.")
                return _download_execution_data(download_uri)

            if job_state in ("FAILED", "ERROR", "CANCELLED"):
                raise GCClientError(f"Execution data job failed with state: {job_state}")

            time.sleep(_POLL_INTERVAL)
        except ApiException as e:
            raise GCClientError(f"Error polling job '{job_id}': {e.status} {e.reason}") from e

    raise GCClientError(
        f"Execution data job '{job_id}' did not complete within "
        f"{_POLL_MAX_ATTEMPTS * _POLL_INTERVAL} seconds."
    )


def get_raw_debug_data(conversation_id: str, org: dict | None = None) -> dict:
    """
    Debug helper: returns raw GC API responses at each pipeline stage.
    Useful for inspecting actual key names returned by the SDK before
    adjusting flow_parser.py mappings.
    """
    org = _resolve_org_dict(org)
    result = {"conversationId": conversation_id, "stages": {}}
    api_client = _get_api_client(org)

    # Stage 1: conversation details
    try:
        analytics_api = gc.ConversationsApi(api_client)
        conv = analytics_api.get_analytics_conversation_details(conversation_id)
        conv_dict = conv.to_dict()
        result["stages"]["conversationDetails"] = conv_dict

        instances = _extract_flow_instance_ids(conv_dict)
        result["stages"]["extractedInstances"] = instances
    except ApiException as e:
        result["stages"]["conversationDetailsError"] = f"{e.status} {e.reason}"
        return result

    # Stage 2: for each instance, start job and capture raw job result
    result["stages"]["executionJobs"] = []
    flow_api = gc.ArchitectApi(api_client)

    for inst in instances:
        instance_id = inst["flowInstanceId"]
        job_entry = {"instanceId": instance_id, "flowName": inst.get("flowName")}
        try:
            inst_result = flow_api.get_flows_instance(instance_id, expand="execution")
            inst_dict = inst_result.to_dict()
            job_entry["instanceRawKeys"] = list(inst_dict.keys())
            job_id = (
                inst_result.job_id
                if hasattr(inst_result, "job_id")
                else (inst_dict.get("job_id") or inst_dict.get("jobId"))
            )
            job_entry["jobId"] = job_id
            if job_id:
                job_result = flow_api.get_flows_instances_job(job_id)
                job_dict = job_result.to_dict()
                job_entry["jobStatus"] = job_dict.get("status")
                job_entry["jobRawTopKeys"] = list(job_dict.keys())
                # Show structure one level deep
                for k, v in job_dict.items():
                    if isinstance(v, dict):
                        job_entry[f"jobKey_{k}_subkeys"] = list(v.keys())
                    elif isinstance(v, list) and v:
                        job_entry[f"jobKey_{k}_firstItemKeys"] = (
                            list(v[0].keys()) if isinstance(v[0], dict) else str(v[0])[:100]
                        )
                job_entry["jobRawSample"] = job_dict
        except ApiException as e:
            job_entry["error"] = f"{e.status} {e.reason}"

        result["stages"]["executionJobs"].append(job_entry)

    return result


def get_flow_execution_data(conversation_id: str, org: dict | None = None) -> dict:
    """
    Main entry point. Given a ConversationId:
    1. Fetch conversation details → extract flowInstanceId(s)
    2. For each instance: start async job → poll → return execution data
    Returns a structured dict ready for flow_parser.

    org: dict with keys name/environment/client_id/client_secret.
         If None, falls back to single-org config values.
    """
    org = _resolve_org_dict(org)
    api_client = _get_api_client(org)

    # Step 1: Get conversation details and extract flow instances
    logger.info("Fetching conversation details for %s (org: %s)", conversation_id, org["name"])
    conv_details = get_conversation_details(conversation_id, org)
    instances = _extract_flow_instance_ids(conv_details)

    if not instances:
        # Analytics API didn't include flow_instance_id — try Flows Instances API directly
        logger.info("Analytics returned no flow_instance_id, trying Flows Instances API fallback")
        instances = _lookup_instances_by_conversation(api_client, conversation_id)

    if not instances:
        raise GCClientError(
            f"No flow execution instances found for conversation '{conversation_id}'. "
            "The conversation passed through a flow, but no execution instance ID was found. "
            "Check that Execution Data logging is enabled in Architect "
            "(flow Settings → Execution Data → 'All') and the flow has been published after that change."
        )

    # Sort chronologically — main flow starts first, inqueue/secondary flows later
    instances.sort(key=lambda i: i.get("startTime") or "")
    logger.info("Found %d flow instance(s) (sorted by startTime)", len(instances))

    # Step 2: For each instance, download execution data
    results = []
    for inst in instances:
        instance_id = inst["flowInstanceId"]
        logger.info("Fetching execution data for instance %s (%s)", instance_id, inst.get("flowName", ""))

        job_id, execution_data = _start_execution_data_job(api_client, instance_id)
        if execution_data is None:
            execution_data = _poll_job(api_client, job_id)

        results.append({
            "instanceMeta": inst,
            "executionData": execution_data,
        })

    return {
        "conversationId": conversation_id,
        "conversationDetails": conv_details,
        "flowInstances": results,
    }
