import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Single-org / local dev (backward compatible) ───────────────────
GC_CLIENT_ID     = os.getenv("GC_CLIENT_ID", "")
GC_CLIENT_SECRET = os.getenv("GC_CLIENT_SECRET", "")
GC_ENVIRONMENT   = os.getenv("GC_ENVIRONMENT", "mypurecloud.com")

# ── App server ─────────────────────────────────────────────────────
APP_HOST      = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT      = int(os.getenv("APP_PORT", "8000"))
APP_DEBUG     = os.getenv("APP_DEBUG", "false").lower() == "true"
APP_ROOT_PATH = os.getenv("APP_ROOT_PATH", "").rstrip("/")

# ── Auth ───────────────────────────────────────────────────────────
APP_PASSWORD_HASH = os.getenv("APP_PASSWORD_HASH", "")
SESSION_SECRET    = os.getenv("SESSION_SECRET", "dev-secret-change-in-prod")

# ── Multi-org ──────────────────────────────────────────────────────
FC_ENCRYPTION_KEY = os.getenv("FC_ENCRYPTION_KEY", "")
ORGS_FILE         = os.getenv("ORGS_FILE", str(Path(__file__).parent.parent / "data" / "orgs.yaml"))

MULTI_ORG_MODE = bool(FC_ENCRYPTION_KEY and Path(ORGS_FILE).exists())
