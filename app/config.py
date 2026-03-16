import os
from dotenv import load_dotenv

load_dotenv()

GC_CLIENT_ID = os.getenv("GC_CLIENT_ID", "")
GC_CLIENT_SECRET = os.getenv("GC_CLIENT_SECRET", "")
GC_ENVIRONMENT = os.getenv("GC_ENVIRONMENT", "mypurecloud.com")

APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
APP_DEBUG = os.getenv("APP_DEBUG", "false").lower() == "true"

if not GC_CLIENT_ID or not GC_CLIENT_SECRET:
    raise EnvironmentError(
        "GC_CLIENT_ID and GC_CLIENT_SECRET must be set in .env file"
    )
