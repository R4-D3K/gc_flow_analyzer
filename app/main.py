"""
GC Flow Analyzer — FastAPI application entry point.
"""

import logging
import re
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app import config
from app.auth import verify_password, is_authenticated
from app.gc_client import get_flow_execution_data, get_raw_debug_data, GCClientError
from app.flow_parser import parse_execution_data, _flatten_execution_items

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="GC Flow Analyzer", version="1.0.0")

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["root"] = config.APP_ROOT_PATH


# ── Middleware stack (last added = outermost = runs first) ─────────
# 1. Auth middleware — runs last before route handler
class _AuthMiddleware(BaseHTTPMiddleware):
    _PUBLIC = {"/login", "/login/", "/health", "/health/"}

    async def dispatch(self, request: Request, call_next):
        if request.scope.get("path", "/") in self._PUBLIC or not config.APP_PASSWORD_HASH:
            return await call_next(request)
        if not is_authenticated(request):
            return RedirectResponse(url=f"{config.APP_ROOT_PATH}/login", status_code=302)
        return await call_next(request)

app.add_middleware(_AuthMiddleware)  # added 1st → runs 3rd

# 2. Session middleware
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET,
    max_age=8 * 3600,
    https_only=not config.APP_DEBUG,
    same_site="lax",
)  # added 2nd → runs 2nd

# 3. Path-prefix stripping (only when APP_ROOT_PATH is set)
if config.APP_ROOT_PATH:
    class _StripPrefix(BaseHTTPMiddleware):
        _prefix = config.APP_ROOT_PATH

        async def dispatch(self, request: Request, call_next):
            path = request.scope.get("path", "/")
            if path == self._prefix:
                request.scope["path"] = "/"
                request.scope["raw_path"] = b"/"
            elif path.startswith(self._prefix + "/"):
                new = path[len(self._prefix):]
                request.scope["path"] = new
                request.scope["raw_path"] = new.encode()
            return await call_next(request)

    app.add_middleware(_StripPrefix)  # added 3rd → runs 1st


# ── Startup ────────────────────────────────────────────────────────
@app.on_event("startup")
async def _startup():
    if config.MULTI_ORG_MODE:
        from app.orgs import load_orgs, get_orgs
        load_orgs()
        logger.info("Multi-org mode: %d org(s) loaded", len(get_orgs()))
    elif config.GC_CLIENT_ID:
        logger.info("Single-org mode: %s", config.GC_ENVIRONMENT)
    else:
        logger.warning("No GC credentials configured")


def _get_orgs():
    if config.MULTI_ORG_MODE:
        from app.orgs import get_orgs
        return get_orgs()
    return []


def _resolve_org(name: str | None) -> dict | None:
    if not config.MULTI_ORG_MODE:
        if config.GC_CLIENT_ID:
            return {"name": "Default", "environment": config.GC_ENVIRONMENT,
                    "client_id": config.GC_CLIENT_ID, "client_secret": config.GC_CLIENT_SECRET}
        return None
    if name:
        from app.orgs import get_org
        return get_org(name)
    orgs = _get_orgs()
    return orgs[0] if orgs else None


# ── Auth routes ────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url=f"{config.APP_ROOT_PATH}/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login", response_class=HTMLResponse)
async def login_post(request: Request, password: str = Form(...)):
    if verify_password(password):
        request.session["authenticated"] = True
        return RedirectResponse(url=f"{config.APP_ROOT_PATH}/", status_code=302)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Invalid password."}
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url=f"{config.APP_ROOT_PATH}/login", status_code=302)


# ── Home ───────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    orgs = _get_orgs()
    org_names = [o["name"] for o in orgs]
    selected = request.session.get("selected_org") or (org_names[0] if org_names else "")
    return templates.TemplateResponse("index.html", {
        "request": request,
        "org_names": org_names,
        "selected_org": selected,
        "multi_org": config.MULTI_ORG_MODE,
    })


# ── Analyze ────────────────────────────────────────────────────────
def _index_tpl(request: Request, error: str = "", last_id: str = "", org_name: str = ""):
    orgs = _get_orgs()
    org_names = [o["name"] for o in orgs]
    selected = org_name or request.session.get("selected_org") or (org_names[0] if org_names else "")
    return templates.TemplateResponse("index.html", {
        "request": request, "error": error, "last_id": last_id,
        "org_names": org_names, "selected_org": selected, "multi_org": config.MULTI_ORG_MODE,
    })


@app.post("/analyze", response_class=HTMLResponse)
async def analyze(request: Request, conversation_id: str = Form(...), org_name: str = Form("")):
    conversation_id = conversation_id.strip()
    if not conversation_id:
        return _index_tpl(request, "Please enter a Conversation ID.", org_name=org_name)
    if not _UUID_RE.match(conversation_id):
        return _index_tpl(request,
            "Invalid Conversation ID format. Expected a UUID like: f3ba9cc0-6475-4c6f-ad67-d431c75e27d5",
            last_id=conversation_id, org_name=org_name)

    if org_name:
        request.session["selected_org"] = org_name

    org = _resolve_org(org_name or request.session.get("selected_org"))
    if org is None:
        return _index_tpl(request, "No org configured. Add orgs via manage_orgs.py.", org_name=org_name)

    logger.info("Analyzing %s (org: %s)", conversation_id, org["name"])
    try:
        raw_data = get_flow_execution_data(conversation_id, org)
        parsed = parse_execution_data(raw_data)
    except GCClientError as e:
        return _index_tpl(request, str(e), last_id=conversation_id, org_name=org_name)
    except Exception as e:
        logger.exception("Unexpected error for %s", conversation_id)
        return _index_tpl(request, f"Unexpected error: {e}", last_id=conversation_id, org_name=org_name)

    return templates.TemplateResponse("result.html", {"request": request, "conversation": parsed})


@app.get("/analyze/{conversation_id}", response_class=HTMLResponse)
async def analyze_get(request: Request, conversation_id: str):
    if not _UUID_RE.match(conversation_id.strip()):
        return RedirectResponse(url=f"{config.APP_ROOT_PATH}/")
    return await analyze(request, conversation_id=conversation_id, org_name="")


# ── JSON API ───────────────────────────────────────────────────────
@app.get("/api/analyze/{conversation_id}", response_class=JSONResponse)
async def analyze_api(request: Request, conversation_id: str):
    org = _resolve_org(request.session.get("selected_org"))
    try:
        raw_data = get_flow_execution_data(conversation_id, org)
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
                        {"sequence": s.sequence, "actionId": s.action_id,
                         "actionName": s.action_name, "actionType": s.action_type,
                         "startTime": s.start_time, "endTime": s.end_time,
                         "durationMs": s.duration_ms, "inputs": s.inputs,
                         "outputs": s.outputs, "error": s.error}
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
        logger.exception("API error for %s", conversation_id)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/rawsteps/{conversation_id}", response_class=JSONResponse)
async def raw_steps(request: Request, conversation_id: str):
    org = _resolve_org(request.session.get("selected_org"))
    try:
        raw_data = get_flow_execution_data(conversation_id.strip(), org)
        result = []
        for flow_inst in raw_data.get("flowInstances", []):
            exec_data = flow_inst.get("executionData", {})
            items = _flatten_execution_items(exec_data)
            result.append({"flowName": flow_inst.get("instanceMeta", {}).get("flowName"),
                           "itemCount": len(items), "firstItem": items[0] if items else None,
                           "allItems": items})
        return result
    except GCClientError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/debug/{conversation_id}", response_class=JSONResponse)
async def debug_raw(request: Request, conversation_id: str):
    org = _resolve_org(request.session.get("selected_org"))
    try:
        return get_raw_debug_data(conversation_id.strip(), org)
    except GCClientError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/health")
async def health():
    return {"status": "ok"}
