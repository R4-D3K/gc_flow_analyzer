"""
Genesys Cloud API client — lightweight httpx implementation.

Replaces PureCloudPlatformClientV2 SDK with direct REST calls via httpx.
Memory footprint: ~80 MB vs ~400 MB with the SDK.

Handles:
- OAuth2 Client Credentials token management (cached, auto-refresh)
- Analytics conversation details
- Flow instances query (fallback when analytics lacks flowInstanceId)
- Async execution data job (start → poll → download)
"""

import time
import logging

import httpx

from app import config

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 2
_POLL_MAX_ATTEMPTS = 30
_TOKEN_EXPIRY_SECONDS = 3600
_TOKEN_REFRESH_BUFFER = 60

# Token cache: client_id → (access_token, expires_at_monotonic)
_token_cache: dict[str, tuple[str, float]] = {}


class GCClientError(Exception):
    """Raised when a Genesys Cloud API call fails."""


def _resolve_org_dict(org: dict | None) -> dict:
    if org is not None:
        return org
    return {
        "name":          "Default",
        "environment":   config.GC_ENVIRONMENT,
        "client_id":     config.GC_CLIENT_ID,
        "client_secret": config.GC_CLIENT_SECRET,
    }


def _get_token(org: dict) -> str:
    """Return a valid Bearer token, reusing cache when possible."""
    client_id = org["client_id"]
    now = time.monotonic()

    cached = _token_cache.get(client_id)
    if cached is not None:
        token, expires_at = cached
        if now < expires_at:
            logger.debug("Reusing cached token for org '%s'", org.get("name"))
            return token

    logger.info("Fetching new GC token for org '%s'", org.get("name"))
    url = f"https://login.{org['environment']}/oauth/token"
    try:
        resp = httpx.post(
            url,
            data={"grant_type": "client_credentials"},
            auth=(org["client_id"], org["client_secret"]),
            timeout=15.0,
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]
        _token_cache[client_id] = (token, now + _TOKEN_EXPIRY_SECONDS - _TOKEN_REFRESH_BUFFER)
        logger.info("GC authentication successful for org '%s'", org.get("name"))
        return token
    except httpx.HTTPStatusError as e:
        raise GCClientError(
            f"Authentication failed: {e.response.status_code} {e.response.text[:200]}"
        ) from e
    except Exception as e:
        raise GCClientError(f"Authentication error: {e}") from e


def _api_get(org: dict, path: str, params: dict | None = None) -> dict:
    """Authenticated GET to the GC REST API."""
    token = _get_token(org)
    url = f"https://api.{org['environment']}{path}"
    try:
        resp = httpx.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        if resp.status_code == 404:
            raise GCClientError(f"Not found: {path}")
        resp.raise_for_status()
        return resp.json()
    except GCClientError:
        raise
    except httpx.HTTPStatusError as e:
        raise GCClientError(f"API error {e.response.status_code}: {path}") from e
    except Exception as e:
        raise GCClientError(f"Request failed [{path}]: {e}") from e


def _api_post(org: dict, path: str, body: dict) -> dict:
    """Authenticated POST to the GC REST API."""
    token = _get_token(org)
    url = f"https://api.{org['environment']}{path}"
    try:
        resp = httpx.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        raise GCClientError(f"API error {e.response.status_code}: {path}") from e
    except Exception as e:
        raise GCClientError(f"Request failed [{path}]: {e}") from e


def get_conversation_details(conversation_id: str, org: dict) -> dict:
    """Fetch conversation details from Analytics API."""
    logger.info("Fetching conversation details for %s", conversation_id)
    return _api_get(org, f"/api/v2/analytics/conversations/{conversation_id}/details")


def _extract_flow_instance_ids(conversation_details: dict) -> list[dict]:
    """Walk participants/sessions to collect flow instance IDs."""
    participants = conversation_details.get("participants", [])
    logger.info("_extract_flow_instance_ids: %d participant(s)", len(participants))

    instances = []
    for participant in participants:
        for session in participant.get("sessions", []):
            # GC API returns either singular 'flow' (dict) or plural 'flows' (list)
            flow_singular = session.get("flow")
            flows_plural = session.get("flows") or []
            if flow_singular and isinstance(flow_singular, dict):
                flows_raw = [flow_singular]
            elif flows_plural:
                flows_raw = flows_plural if isinstance(flows_plural, list) else [flows_plural]
            else:
                flows_raw = []

            for flow in flows_raw:
                instance_id = flow.get("flowInstanceId") or flow.get("flow_instance_id")
                if not instance_id:
                    logger.warning(
                        "Flow '%s' has no flowInstanceId — "
                        "Execution Data logging may be disabled in Architect",
                        flow.get("flowName") or flow.get("flow_name", "?"),
                    )
                    continue
                if instance_id not in [i["flowInstanceId"] for i in instances]:
                    instances.append({
                        "flowInstanceId": instance_id,
                        "flowId":      flow.get("flowId")      or flow.get("flow_id", ""),
                        "flowName":    flow.get("flowName")    or flow.get("flow_name", ""),
                        "flowType":    flow.get("flowType")    or flow.get("flow_type", ""),
                        "flowVersion": flow.get("flowVersion") or flow.get("flow_version", ""),
                        "startTime":   flow.get("startTime")   or flow.get("flow_start_timestamp", ""),
                        "endTime":     flow.get("endTime")     or flow.get("flow_end_timestamp", ""),
                        "exitReason":  flow.get("exitReason")  or flow.get("exit_reason", ""),
                    })

    logger.info("_extract_flow_instance_ids: found %d instance(s)", len(instances))
    return instances


def _lookup_instances_by_conversation(org: dict, conversation_id: str) -> list[dict]:
    """Fallback: POST /api/v2/flows/instances/query filtered by conversationId."""
    try:
        body = {
            "query": [
                {
                    "criteria": {
                        "key": "ConversationId",
                        "operator": "eq",
                        "value": conversation_id,
                    }
                }
            ]
        }
        result = _api_post(org, "/api/v2/flows/instances/query", body)
        entities = result.get("entities") or []
        logger.info("flows/instances/query fallback: %d entity(ies)", len(entities))

        instances = []
        for ent in entities:
            instance_id = ent.get("id")
            if instance_id and instance_id not in [i["flowInstanceId"] for i in instances]:
                instances.append({
                    "flowInstanceId": instance_id,
                    "flowId":      ent.get("flowId", ""),
                    "flowName":    ent.get("flowName", ""),
                    "flowType":    ent.get("flowType", ""),
                    "flowVersion": ent.get("flowVersion", ""),
                    "startTime":   str(ent.get("startDateTime", "")),
                    "endTime":     str(ent.get("endDateTime", "")),
                    "exitReason":  ent.get("flowErrorReason") or ent.get("flowWarningReason", ""),
                })
        return instances
    except GCClientError as e:
        logger.warning("flows/instances/query fallback failed: %s", e)
        return []


def _start_execution_data_job(org: dict, instance_id: str) -> tuple[str, dict | None]:
    """
    GET /api/v2/flows/instances/{instanceId}?expand=execution
    Returns (job_id, execution_data_or_None).
    If the job is already fulfilled, downloads data immediately.
    """
    result = _api_get(org, f"/api/v2/flows/instances/{instance_id}", params={"expand": "execution"})
    job_id = result.get("id") or result.get("jobId")
    job_state = (result.get("jobState") or result.get("job_state") or "").upper()
    entities = result.get("entities") or []

    logger.info("Start job: id=%s state=%s entities=%d", job_id, job_state, len(entities))

    if not job_id:
        raise GCClientError(f"No job ID returned for instance '{instance_id}'.")

    if job_state in ("FULFILLED", "SUCCESS") and entities:
        download_uri = entities[0].get("downloadUri") or entities[0].get("download_uri")
        if download_uri:
            logger.info("Job already fulfilled, downloading immediately")
            return job_id, _download_execution_data(download_uri)

    return job_id, None


def _download_execution_data(download_uri: str) -> dict:
    """Download execution data JSON from pre-signed URI."""
    logger.info("Downloading execution data from: %s", download_uri[:80])
    try:
        resp = httpx.get(download_uri, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
        logger.info("Execution data downloaded, top-level keys: %s",
                    list(data.keys()) if isinstance(data, dict) else f"list[{len(data)}]")
        return data
    except Exception as e:
        raise GCClientError(f"Failed to download execution data: {e}") from e


def _poll_job(org: dict, job_id: str) -> dict:
    """Poll GET /api/v2/flows/instances/jobs/{jobId} until fulfilled."""
    for attempt in range(_POLL_MAX_ATTEMPTS):
        result = _api_get(org, f"/api/v2/flows/instances/jobs/{job_id}")
        job_state = (result.get("jobState") or result.get("job_state") or "").upper()
        logger.info("Job %s state: %s (attempt %d/%d)", job_id, job_state, attempt + 1, _POLL_MAX_ATTEMPTS)

        if job_state in ("FULFILLED", "SUCCESS"):
            entities = result.get("entities") or []
            if not entities:
                raise GCClientError("Job fulfilled but no entities returned.")
            entity = entities[0]
            if entity.get("failed"):
                raise GCClientError(f"Execution data entity failed: {entity.get('statusCode')}")
            download_uri = entity.get("downloadUri") or entity.get("download_uri")
            if not download_uri:
                raise GCClientError("Job fulfilled but no downloadUri in entity.")
            return _download_execution_data(download_uri)

        if job_state in ("FAILED", "ERROR", "CANCELLED"):
            raise GCClientError(f"Execution data job failed with state: {job_state}")

        time.sleep(_POLL_INTERVAL)

    raise GCClientError(
        f"Job '{job_id}' did not complete within {_POLL_MAX_ATTEMPTS * _POLL_INTERVAL}s."
    )


def get_raw_debug_data(conversation_id: str, org: dict | None = None) -> dict:
    """Debug helper: returns raw API responses at each pipeline stage."""
    org = _resolve_org_dict(org)
    result = {"conversationId": conversation_id, "stages": {}}

    try:
        conv = get_conversation_details(conversation_id, org)
        result["stages"]["conversationDetails"] = conv
        instances = _extract_flow_instance_ids(conv)
        result["stages"]["extractedInstances"] = instances
    except GCClientError as e:
        result["stages"]["conversationDetailsError"] = str(e)
        return result

    result["stages"]["executionJobs"] = []
    for inst in instances:
        instance_id = inst["flowInstanceId"]
        job_entry = {"instanceId": instance_id, "flowName": inst.get("flowName")}
        try:
            raw = _api_get(org, f"/api/v2/flows/instances/{instance_id}", params={"expand": "execution"})
            job_entry["instanceRawKeys"] = list(raw.keys())
            job_id = raw.get("id") or raw.get("jobId")
            job_entry["jobId"] = job_id
            if job_id:
                job_raw = _api_get(org, f"/api/v2/flows/instances/jobs/{job_id}")
                job_entry["jobRawTopKeys"] = list(job_raw.keys())
                job_entry["jobState"] = job_raw.get("jobState")
                job_entry["jobRawSample"] = job_raw
        except GCClientError as e:
            job_entry["error"] = str(e)
        result["stages"]["executionJobs"].append(job_entry)

    return result


def get_flow_execution_data(conversation_id: str, org: dict | None = None) -> dict:
    """
    Main entry point. Given a ConversationId:
    1. Fetch conversation details → extract flowInstanceId(s)
    2. For each instance: start async job → poll → download execution data
    """
    org = _resolve_org_dict(org)

    logger.info("Processing conversation %s (org: %s)", conversation_id, org["name"])
    conv_details = get_conversation_details(conversation_id, org)
    instances = _extract_flow_instance_ids(conv_details)

    if not instances:
        logger.info("No flowInstanceId in analytics, trying flows/instances/query fallback")
        instances = _lookup_instances_by_conversation(org, conversation_id)

    if not instances:
        raise GCClientError(
            f"No flow execution instances found for conversation '{conversation_id}'. "
            "Check that Execution Data logging is enabled in Architect "
            "(flow Settings → Execution Data → 'All') and re-publish the flow."
        )

    instances.sort(key=lambda i: i.get("startTime") or "")
    logger.info("Processing %d flow instance(s)", len(instances))

    results = []
    for inst in instances:
        instance_id = inst["flowInstanceId"]
        logger.info("Fetching execution data for %s (%s)", instance_id, inst.get("flowName", ""))
        job_id, execution_data = _start_execution_data_job(org, instance_id)
        if execution_data is None:
            execution_data = _poll_job(org, job_id)
        results.append({"instanceMeta": inst, "executionData": execution_data})

    return {
        "conversationId": conversation_id,
        "conversationDetails": conv_details,
        "flowInstances": results,
    }
