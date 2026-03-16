"""
Genesys Cloud API client wrapper.

Handles authentication (Client Credentials) and all API calls needed
to retrieve flow execution data for a given ConversationId.
"""

import time
import logging
from typing import Optional

import PureCloudPlatformClientV2 as gc
from PureCloudPlatformClientV2.rest import ApiException

from app.config import GC_CLIENT_ID, GC_CLIENT_SECRET, GC_ENVIRONMENT

logger = logging.getLogger(__name__)

# How long (seconds) to wait between job status polls
_POLL_INTERVAL = 2
# Maximum number of poll attempts before giving up (~60 seconds total)
_POLL_MAX_ATTEMPTS = 30


class GCClientError(Exception):
    """Raised when a Genesys Cloud API call fails."""


def _get_api_client() -> gc.ApiClient:
    """Authenticate via Client Credentials and return a configured ApiClient."""
    gc.configuration.host = f"https://api.{GC_ENVIRONMENT}"
    api_client = gc.api_client.ApiClient()

    try:
        token_response = api_client.get_client_credentials_token(
            GC_CLIENT_ID, GC_CLIENT_SECRET
        )
        api_client.set_default_header("Authorization", f"Bearer {token_response.access_token}")
        logger.info("Genesys Cloud authentication successful")
        return api_client
    except ApiException as e:
        raise GCClientError(f"Authentication failed: {e.status} {e.reason}") from e


def get_conversation_details(conversation_id: str) -> dict:
    """
    Fetch conversation details from Analytics API.
    Returns the raw dict from AnalyticsConversationWithoutAttributes.
    """
    api_client = _get_api_client()
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
    instances = []
    for participant in conversation_details.get("participants", []):
        for session in participant.get("sessions", []):
            for flow in session.get("flows", []):
                instance_id = flow.get("flow_instance_id") or flow.get("flowInstanceId")
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
    return instances


def _start_execution_data_job(api_client: gc.ApiClient, instance_id: str) -> str:
    """
    Start an async job to download execution data for a flow instance.
    Returns the jobId.
    """
    flow_api = gc.ArchitectApi(api_client)
    try:
        result = flow_api.get_flows_instance(instance_id, expand="execution")
        job_id = result.job_id if hasattr(result, "job_id") else (result.to_dict().get("job_id") or result.to_dict().get("jobId"))
        if not job_id:
            raise GCClientError("No jobId returned when starting execution data job.")
        logger.info("Started execution job %s for instance %s", job_id, instance_id)
        return job_id
    except ApiException as e:
        raise GCClientError(
            f"Failed to start execution job for instance '{instance_id}': {e.status} {e.reason}"
        ) from e


def _poll_job(api_client: gc.ApiClient, job_id: str) -> dict:
    """
    Poll the job endpoint until it completes. Returns the execution data dict.
    """
    flow_api = gc.ArchitectApi(api_client)
    for attempt in range(_POLL_MAX_ATTEMPTS):
        try:
            result = flow_api.get_flows_instances_job(job_id)
            result_dict = result.to_dict()
            status = result_dict.get("status", "").upper()
            logger.debug("Job %s status: %s (attempt %d)", job_id, status, attempt + 1)

            if status == "FULFILLED":
                return result_dict
            if status in ("FAILED", "ERROR", "CANCELLED"):
                raise GCClientError(f"Execution data job failed with status: {status}")

            time.sleep(_POLL_INTERVAL)
        except ApiException as e:
            raise GCClientError(f"Error polling job '{job_id}': {e.status} {e.reason}") from e

    raise GCClientError(
        f"Execution data job '{job_id}' did not complete within "
        f"{_POLL_MAX_ATTEMPTS * _POLL_INTERVAL} seconds."
    )


def get_raw_debug_data(conversation_id: str) -> dict:
    """
    Debug helper: returns raw GC API responses at each pipeline stage.
    Useful for inspecting actual key names returned by the SDK before
    adjusting flow_parser.py mappings.
    """
    result = {"conversationId": conversation_id, "stages": {}}
    api_client = _get_api_client()

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


def get_flow_execution_data(conversation_id: str) -> dict:
    """
    Main entry point. Given a ConversationId:
    1. Fetch conversation details → extract flowInstanceId(s)
    2. For each instance: start async job → poll → return execution data
    Returns a structured dict ready for flow_parser.
    """
    api_client = _get_api_client()

    # Step 1: Get conversation details and extract flow instances
    logger.info("Fetching conversation details for %s", conversation_id)
    conv_details = get_conversation_details(conversation_id)
    instances = _extract_flow_instance_ids(conv_details)

    if not instances:
        raise GCClientError(
            f"No flow execution instances found for conversation '{conversation_id}'. "
            "Make sure the flow has execution data logging enabled."
        )

    logger.info("Found %d flow instance(s)", len(instances))

    # Step 2: For each instance, download execution data
    results = []
    for inst in instances:
        instance_id = inst["flowInstanceId"]
        logger.info("Fetching execution data for instance %s (%s)", instance_id, inst.get("flowName", ""))

        job_id = _start_execution_data_job(api_client, instance_id)
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
