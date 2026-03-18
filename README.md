# GC Flow Analyzer

Web tool for analyzing Genesys Cloud Architect flow execution history via the Public API.

## Features

- Step-by-step execution trace with durations, inputs/outputs, and nested data-action sub-steps
- Variable change history pivot view grouped by prefix (Flow, Task, Call, …)
- Interactive SVG flowchart with zoom/pan, node detail panel, and variable state snapshot per step
- Multi-org support — select from multiple GC organizations at runtime
- Password-protected login with session management
- Docker-ready for self-hosted / Synology NAS deployment

---

## Quick start (local / single-org)

### Prerequisites

- Python 3.11+
- Genesys Cloud OAuth Client (Client Credentials) with:
  - `Analytics > Conversation Detail > View`
  - `Architect > Flow > View`
  - `Architect > Flow Execution > View`
  - `Architect > flowInstance > All Permissions`
  - `Architect > flowInstanceExecutionData > All Permissions`

### Setup

1. Copy `.env.example` to `.env`:

   ```bat
   copy .env.example .env
   ```

2. Edit `.env` and fill in your credentials:

   ```env
   GC_CLIENT_ID=your-client-id
   GC_CLIENT_SECRET=your-client-secret
   GC_ENVIRONMENT=mypurecloud.com
   ```

3. Run the app:

   ```bat
   start.bat
   ```

4. Open your browser at <http://127.0.0.1:8000>

---

## Production deployment (Docker + multi-org)

See [DEPLOYMENT.md](DEPLOYMENT.md) for a detailed step-by-step guide covering:

- Docker image build and `docker-compose.yml`
- Synology NAS / Container Manager setup
- Multi-org encrypted credential management via `manage_orgs.py`
- Reverse proxy and SSL (Let's Encrypt) configuration
- Login page and session security

### Org management CLI

```bash
# Generate a Fernet encryption key (run once, save to .env.prod)
python manage_orgs.py generate-key

# Generate a bcrypt password hash for the login page
python manage_orgs.py hash-password

# Add an org profile (credentials are Fernet-encrypted at rest)
python manage_orgs.py add --name "Customer A" --environment mypurecloud.ie \
    --client-id CLIENT_ID --client-secret CLIENT_SECRET

# List configured orgs
python manage_orgs.py list

# Remove an org
python manage_orgs.py delete --name "Customer A"
```

---

## Usage

1. Find a Conversation ID in:
   - Analytics > Conversation Detail
   - Architect > Execution History (shown as `Call.ConversationId` in the Data panel)

2. In multi-org mode, select the target organization from the dropdown, then paste the Conversation ID and click **Analyze**.
   The 10 most recently analyzed IDs are saved in browser local storage for quick re-access.

3. The result page shows three tabs per flow instance:

   - **Steps** — full step-by-step execution trace with durations, I/O data, and nested data-action sub-steps. Click any row to open a detail panel. Filterable by name or type; exportable to CSV.
   - **Variable Changes** — pivot view of all output variable changes grouped by prefix (Flow, Task, Call, …). Shows the full value history for each variable as a chip sequence. Filterable; exportable to CSV.
   - **Flow Diagram** — interactive SVG flowchart with split layout: graph on the left, step detail panel on the right. Node shape and color indicate action category (decision = diamond, meta = capsule, action = rectangle). Click any node to open the detail panel showing inputs, outputs, and a snapshot of all variable values at that point in execution. Supports zoom/pan (mouse wheel + drag), fit-to-view, and four background themes (Light / Dark / Beige / Gray) persisted across sessions.

---

## Keyboard shortcuts

| Key   | Action                  |
| ----- | ----------------------- |
| `Esc` | Close step detail panel |

---

## API endpoints

| Endpoint                             | Description                                                     |
| ------------------------------------ | --------------------------------------------------------------- |
| `GET /api/analyze/{conversationId}`  | Parsed execution data as JSON                                   |
| `GET /api/rawsteps/{conversationId}` | Raw (unparsed) execution items — useful for debugging key names |
| `GET /api/debug/{conversationId}`    | Unprocessed GC API responses at each pipeline stage             |

---

## Architecture

```text
app/
├── main.py          FastAPI routes, middleware (auth, session, path prefix)
├── config.py        Environment / settings
├── auth.py          Password verification and session check
├── orgs.py          Multi-org loader — Fernet-decrypts credentials from orgs.yaml
├── gc_client.py     Genesys Cloud API calls (auth, conversation details, execution job)
├── flow_parser.py   Transforms raw execution JSON → Python dataclasses
└── templates/
    ├── base.html    Layout, Tailwind CSS
    ├── login.html   Login page
    ├── index.html   Input form with org selector and recent-ID history
    └── result.html  Analysis result (Steps · Variable Changes · Flow Diagram)

manage_orgs.py       CLI for managing encrypted org profiles
Dockerfile           Production Docker image (python:3.11-slim, non-root user)
docker-compose.yml   Single-service compose with volume mount and healthcheck
DEPLOYMENT.md        Full production deployment guide
```

---

## Notes

- The flow must have **Execution Data Level: All** enabled in Architect to capture step-level data.
- Execution data retrieval is asynchronous on the GC side; the tool polls until the job completes (max 60 s).
- Access tokens are cached per org for ~59 minutes to avoid unnecessary re-authentication.
- Nested sub-steps from reusable tasks (data actions, sub-tasks) are expanded inline in the Steps table and are visible in the Flow Diagram timeline.
- In single-org mode (no `FC_ENCRYPTION_KEY`), credentials are read directly from environment variables and the login page is disabled.
