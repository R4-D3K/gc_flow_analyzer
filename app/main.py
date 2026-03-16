"""
GC Flow Analyzer — FastAPI application entry point.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from app.gc_client import get_flow_execution_data, get_raw_debug_data, GCClientError
from app.flow_parser import parse_execution_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="GC Flow Analyzer", version="1.0.0")

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Home page — conversation ID input form."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/analyze", response_class=HTMLResponse)
async def analyze(request: Request, conversation_id: str = Form(...)):
    """
    Main analysis endpoint.
    Fetches execution data from Genesys Cloud and renders the result page.
    """
    conversation_id = conversation_id.strip()

    if not conversation_id:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "Please enter a Conversation ID."},
        )

    logger.info("Analyzing conversation: %s", conversation_id)

    try:
        raw_data = get_flow_execution_data(conversation_id)
        parsed = parse_execution_data(raw_data)
    except GCClientError as e:
        logger.warning("GC API error for %s: %s", conversation_id, e)
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": str(e), "last_id": conversation_id},
        )
    except Exception as e:
        logger.exception("Unexpected error analyzing %s", conversation_id)
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "error": f"Unexpected error: {e}",
                "last_id": conversation_id,
            },
        )

    return templates.TemplateResponse(
        "result.html",
        {
            "request": request,
            "conversation": parsed,
        },
    )


@app.get("/api/analyze/{conversation_id}", response_class=JSONResponse)
async def analyze_api(conversation_id: str):
    """
    JSON API endpoint — returns raw parsed data.
    Useful for integration or debugging.
    """
    try:
        raw_data = get_flow_execution_data(conversation_id)
        parsed = parse_execution_data(raw_data)

        return {
            "conversationId": parsed.conversation_id,
            "ani": parsed.ani,
            "calledAddress": parsed.called_address,
            "language": parsed.language,
            "instances": [
                {
                    "instanceId": inst.instance_id,
                    "flowName": inst.flow_name,
                    "flowType": inst.flow_type,
                    "flowVersion": inst.flow_version,
                    "startTime": inst.start_time,
                    "endTime": inst.end_time,
                    "exitReason": inst.exit_reason,
                    "totalSteps": inst.total_steps,
                    "steps": [
                        {
                            "sequence": s.sequence,
                            "actionId": s.action_id,
                            "actionName": s.action_name,
                            "actionType": s.action_type,
                            "startTime": s.start_time,
                            "endTime": s.end_time,
                            "durationMs": s.duration_ms,
                            "inputs": s.inputs,
                            "outputs": s.outputs,
                            "error": s.error,
                        }
                        for s in inst.steps
                    ],
                    "variableTimeline": inst.variable_timeline,
                }
                for inst in parsed.instances
            ],
        }
    except GCClientError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        logger.exception("Unexpected error in API for %s", conversation_id)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/debug/{conversation_id}", response_class=JSONResponse)
async def debug_raw(conversation_id: str):
    """
    Raw debug endpoint — returns unprocessed GC API responses at each stage.
    Use this to inspect actual key names from the SDK before tuning parsers.
    Note: only polls the job once (no waiting for FULFILLED); run again if job is still QUEUED.
    """
    try:
        raw = get_raw_debug_data(conversation_id.strip())
        return raw
    except GCClientError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        logger.exception("Unexpected error in debug endpoint for %s", conversation_id)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/health")
async def health():
    return {"status": "ok"}
