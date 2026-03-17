# GC Flow Analyzer

Web tool for analyzing Genesys Cloud Architect flow execution history via the Public API.

## Prerequisites

- Python 3.11+
- Genesys Cloud OAuth Client (Client Credentials) with the following permissions:
  - `Analytics > Conversation Detail > View`
  - `Architect > Flow > View`
  - `Architect > Flow Execution > View`
  - `Architect > flowInstance > All Permissions`
  - `Architect > flowInstanceExecutionData > All Permissions`

## Setup

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

## Usage

1. Find a Conversation ID in:
   - Analytics > Conversation Detail
   - Architect > Execution History (shown as `Call.ConversationId` in Data panel)

2. Paste the Conversation ID into the input field and click **Analyze**.
   The 10 most recently analyzed IDs are saved in browser local storage for quick re-access.

3. The result page shows three tabs per flow instance:

   - **Steps** — full step-by-step execution trace with durations, I/O data, and nested data-action sub-steps. Click any row to open a detail panel. Filterable by name or type; exportable to CSV.
   - **Variable Changes** — pivot view of all output variable changes grouped by prefix (Flow, Task, Call, …). Shows the full value history for each variable as a chip sequence. Filterable; exportable to CSV.
   - **Flow Diagram** — vertical step timeline with dot markers (shape indicates action category), inline output chips, and slow-step highlighting. Click any step to open the detail panel.

## Keyboard shortcuts

| Key   | Action                   |
| ----- | ------------------------ |
| `Esc` | Close step detail panel  |

## API endpoints

| Endpoint                              | Description                                                      |
| ------------------------------------- | ---------------------------------------------------------------- |
| `GET /api/analyze/{conversationId}`   | Parsed execution data as JSON                                    |
| `GET /api/rawsteps/{conversationId}`  | Raw (unparsed) execution items — useful for debugging key names  |
| `GET /api/debug/{conversationId}`     | Unprocessed GC API responses at each pipeline stage              |

## Architecture

```text
app/
├── main.py          FastAPI routes
├── config.py        Environment / settings
├── gc_client.py     Genesys Cloud API calls (auth, conversation details, execution job)
├── flow_parser.py   Transforms raw execution JSON → Python dataclasses + Mermaid source
└── templates/
    ├── base.html    Layout, Tailwind CSS
    ├── index.html   Input form with recent-ID history
    └── result.html  Analysis result (Steps · Variable Changes · Flow Diagram)
```

## Notes

- The flow must have **Execution Data Level: All** enabled in Architect to capture step-level data.
- Execution data retrieval is asynchronous on the GC side; the tool polls until the job completes (max 60 s).
- Access tokens are cached for ~59 minutes to avoid unnecessary re-authentication on repeated lookups.
- Nested sub-steps from reusable tasks (data actions, sub-tasks) are expanded inline in the Steps table and are visible in the Flow Diagram timeline.
