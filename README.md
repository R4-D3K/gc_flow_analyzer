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
   ```
   copy .env.example .env
   ```

2. Edit `.env` and fill in your credentials:
   ```
   GC_CLIENT_ID=your-client-id
   GC_CLIENT_SECRET=your-client-secret
   GC_ENVIRONMENT=mypurecloud.com
   ```

3. Run the app:
   ```
   start.bat
   ```

4. Open your browser at **http://127.0.0.1:8000**

## Usage

1. Find a Conversation ID in:
   - Analytics > Conversation Detail
   - Architect > Execution History (shown as `Call.ConversationId` in Data panel)

2. Paste the Conversation ID into the input field and click **Analyze**.

3. The result page shows three tabs per flow instance:
   - **Steps** — full step-by-step execution trace with durations, action types, and I/O data
   - **Variable Changes** — timeline of all output variable changes across the run
   - **Flow Diagram** — Mermaid.js visual flowchart of the execution path

## API endpoint

A JSON API is also available for integration or scripting:

```
GET /api/analyze/{conversationId}
```

## Architecture

```
app/
├── main.py          FastAPI routes
├── config.py        Environment / settings
├── gc_client.py     Genesys Cloud API calls (auth, conversation details, execution job)
├── flow_parser.py   Transforms raw execution JSON → Python data classes + Mermaid source
└── templates/
    ├── base.html    Layout, Tailwind CSS, Mermaid.js
    ├── index.html   Input form
    └── result.html  Analysis result (tabs: steps, variables, diagram)
```

## Notes

- The flow must have **Execution Data Level: All** enabled to capture step-level data.
- Execution data retrieval is asynchronous on the GC side; the tool polls until the job completes (max 60s).
- The Mermaid diagram is capped at 80 steps for readability. All steps are still shown in the Steps table.
