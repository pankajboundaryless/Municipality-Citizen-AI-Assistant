# Municipality Citizen AI Assistant

A real-time voice and video AI assistant for municipal citizen services, built with Google Gemini Live API, UiPath Maestro, and Supabase. Citizens can speak naturally to request services (passport renewal, ID card, work permits), upload documents for automatic field extraction, and have their applications submitted directly into the municipal processing system.

## Architecture

```
Browser (WebRTC audio/video)
        ↕ WebSocket
FastAPI Backend (main.py)
        ↕ Gemini Live API (bidiGenerateContent)
        ↕ UiPath Orchestrator  (Maestro job submission)
        ↕ UiPath Document Understanding  (ID/passport extraction)
        ↕ Supabase  (citizen profiles, documents, sessions)
```

## Features

- **Voice-first interaction** — real-time audio streaming via Gemini Live API
- **Document upload & extraction** — citizens upload ID/passport images; UiPath Document Understanding auto-extracts fields
- **Application submission** — Gemini calls `submit_to_municipality` tool which triggers a UiPath Maestro job
- **Citizen profile persistence** — Supabase stores profiles, uploaded documents, and job history
- **Session resumption** — returning citizens are greeted with their previous context
- **OAuth2 login** — Microsoft and Google SSO via Authlib
- **Camera / screen share** — video frames forwarded to Gemini for visual context

## UiPath Components Used

### Agent Type
This solution utilises **both Coded Agents and Low-code Agents**:

- **Coded Agent** — the Python FastAPI backend (`uipath_maestro.py`, `uipath_fetch_id.py`) acts as a coded agent that programmatically calls UiPath Orchestrator REST APIs, manages OAuth2 tokens, uploads documents to storage buckets, starts Maestro jobs, polls for completion, and returns results to the Gemini Live session.
- **Low-code Agents** — the `Municipality_ID_Management` Maestro process (built in UiPath Studio as a BPMN workflow) contains low-code agents that handle the citizen request end-to-end once triggered: triage, ID extraction, data matching, scheduling, and response generation.

### Comprehensive Component List

| UiPath Component | How it is used in this solution |
|---|---|
| **UiPath Maestro** | Orchestrates the entire municipal back-office workflow as a BPMN process. Receives `in_SessionId`, `in_Subject`, `in_CitizenData`, `in_Documents`, and `in_Chat` from the Python backend and returns `out_Reply` to the citizen |
| **Agent Builder** | Used to build the agentic components inside the Maestro process: `Citizen_Response_Agent` (generates citizen-facing replies), `Triage Agent` (classifies request as New / Renew / Stolen ID / Construction Permit / Zoning), and `MatchDataAgent` (matches citizen data against municipal records) |
| **Maestro BPMN Process** | `Municipality_ID_Management` — a BPMN-based low-code workflow that branches by request category, runs IDP extraction, matches data, schedules appointments, submits responses, and sends email confirmations |
| **UiPath Document Understanding (IDP)** | Digitizes uploaded passport/ID images via the DU REST API, selects the correct extractor (`IdentityDocument_passports_v1`), and extracts structured fields (Passport Number, Name, Nationality, Date of Birth, etc.) |
| **IDP Extraction Workflow** | `IDP_ID_Extraction` sub-process inside the Maestro solution — runs document classification and field extraction as a reusable low-code workflow |
| **Deserialize_ID Workflow** | Low-code workflow that parses the `in_CitizenData` JSON argument into typed UiPath variables for use by downstream agents |
| **Orchestrator Storage Buckets** | `Document_Repository` bucket stores uploaded citizen documents at path `{session_id}/{filename}`. Maestro reads files from the bucket using paths passed via `in_Documents` |
| **Orchestrator Folders** | `Shared/Municipality_ID` folder scopes all processes, releases, and storage buckets for this solution |
| **Orchestrator Releases & Jobs** | The Python coded agent resolves the release key at startup, then starts jobs via `StartJobs` API and polls `Jobs({id})` until completion |
| **UiPath Orchestrator REST API** | Called directly from the Python coded agent for: OAuth2 token (`/identity_/connect/token`), folder lookup (`/odata/OrganizationUnits`), release lookup (`/odata/Releases`), bucket resolution (`/odata/Buckets`), file upload (`GetWriteUri` + presigned URL), job start (`StartJobs`), and job polling (`/odata/Jobs`) |
| **API Workflows (HTTP Request)** | Inside the Maestro BPMN process, an HTTP Request activity calls the Supabase REST API to write matched citizen data back to the `citizen_profiles` table |
| **Send Email Activity** | Sends appointment confirmation email to the citizen after the request is processed |
| **Schedule Appointment** | Low-code workflow step that books the citizen's appointment slot within the Maestro process |

## Project Structure

```
├── main.py                  # FastAPI server, WebSocket endpoint, tool handlers
├── gemini_live.py           # Gemini Live API wrapper (audio/video/tool loop)
├── uipath_maestro.py        # UiPath Maestro client (OAuth2, job submission, polling)
├── uipath_fetch_id.py       # UiPath Document Understanding (digitize + extract)
├── supabase_db.py           # Supabase REST API helpers (profiles, documents, sessions)
├── auth.py                  # OAuth2 (Microsoft/Google) login flows
├── db.py                    # SQLite session store (itsdangerous cookie sessions)
├── twilio_handler.py        # Twilio SMS/voice integration (optional)
├── service_requirements.md  # Per-service data requirements injected into system prompt
├── requirements.txt         # Python dependencies
├── Dockerfile               # Container definition for Azure App Service
├── startup.sh               # Azure startup script (gunicorn)
└── frontend/
    ├── index.html           # Citizen-facing UI
    ├── main.js              # App logic, WebSocket orchestration
    ├── gemini-client.js     # WebSocket client for backend
    ├── media-handler.js     # Audio/video capture and playback
    └── pcm-processor.js     # AudioWorklet for PCM processing
```

## Quick Start (Local)

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

Create a `.env` file in the project root:

```env
# Gemini
GEMINI_API_KEY=your_gemini_api_key
# GEMINI_VOICE=Kore        # Optional: Puck, Charon, Kore, Fenrir, Aoede

# UiPath Orchestrator
UIPATH_URL=https://staging.uipath.com
UIPATH_ORGANIZATION=your_org
UIPATH_TENANT=your_tenant
UIPATH_CLIENT_ID=your_client_id
UIPATH_CLIENT_SECRET=your_client_secret
UIPATH_FOLDER_NAME=Shared/Municipality_ID
UIPATH_PROCESS_KEY=Solution.agentic.Municipality_ID_Management
UIPATH_BUCKET_NAME=Document_Repository

# UiPath Document Understanding
UIPATH_DU_PROJECT_ID=your_du_project_guid

# Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=sb_publishable_...        # Publishable key (reads)
SUPABASE_SERVICE_KEY=eyJ...            # Service role key (writes — bypasses RLS)

# OAuth2 (optional)
MICROSOFT_CLIENT_ID=...
MICROSOFT_CLIENT_SECRET=...
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
SESSION_SECRET_KEY=change-me-to-a-long-random-secret
```

### 3. Start the server

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

## UiPath Maestro Integration

The process `Municipality_ID_Management` is triggered via the `submit_to_municipality` tool. Input arguments:

| Argument | Type | Description |
|---|---|---|
| `in_SessionId` | String | Unique chat session ID |
| `in_Subject` | String | Service category (e.g. "Passport Renewal") |
| `in_CitizenData` | String | JSON-serialised citizen profile dict |
| `in_Documents` | String | JSON array of bucket paths: `[{"name": str, "path": str}]` |
| `in_Chat` | String | Full conversation transcript (Agent/User turns) |

Output:

| Argument | Type | Description |
|---|---|---|
| `out_Reply` | String | Response relayed back to the citizen |

Documents are uploaded to the `Document_Repository` storage bucket at `{session_id}/{filename}` before job submission. Maestro reads them from the bucket using the paths in `in_Documents`.

## Supabase Schema

Three tables are used for citizen state persistence:

| Table | Description |
|---|---|
| `citizen_profiles` | Name, email, nationality, date of birth, address, etc. |
| `citizen_documents` | Uploaded document records with DU-extracted fields |
| `citizen_sessions` | UiPath job IDs, job status, and last 30 messages of transcript |

> Write operations require the **service role key** (`SUPABASE_SERVICE_KEY`). The publishable key is read-only due to Row Level Security (RLS).

## Deployment (Azure App Service)

The GitHub Actions workflow (`.github/workflows/main_municipality-citizen-ai.yml`) deploys to Azure on manual trigger:

```bash
gh workflow run main_municipality-citizen-ai.yml --ref main \
  --repo pankajboundaryless/Municipality-Citizen-AI-Assistant
```

Set all `.env` variables as **Application Settings** in Azure Portal → App Service → Configuration.

## Built with Claude Code (AI Coding Agent)

This project was developed using **Claude Code** — Anthropic's AI coding agent — as a core part of the engineering workflow throughout the hackathon.

### (a) Which coding agent tool was used

[Claude Code](https://claude.ai/code) by Anthropic (model: `claude-sonnet-4-6`) was used as the primary coding agent, running inside the VS Code IDE extension. It has access to the full file system, terminal, git, and GitHub CLI, enabling it to read, write, and execute code end-to-end without manual copy-paste.

### (b) How the coding agent contributed to the solution

Claude Code was involved at every layer of the stack — not just as a code generator but as an active debugging and integration partner:

**Architecture & scaffolding**
- Designed and built the full `uipath_maestro.py` client from scratch: OAuth2 token flow, folder/release/bucket resolution, job submission, polling, and output parsing
- Built `uipath_fetch_id.py` for UiPath Document Understanding integration: digitization, extractor selection, field extraction, and result mapping
- Scaffolded the `supabase_db.py` persistence layer covering citizen profiles, uploaded documents, and UiPath job session cache

**UiPath Maestro process templates**
- Defined and validated the exact input/output argument schema for the `Municipality_ID_Management` Maestro process (`in_SessionId`, `in_Subject`, `in_CitizenData`, `in_Documents`, `in_Chat`, `out_Reply`)
- Determined the correct bucket path format (`{session_id}/{filename}`) so Maestro could read uploaded documents from the `Document_Repository` storage bucket

**Gemini Live API integration**
- Built the `gemini_live.py` WebSocket receive loop with concurrent audio/video/text send tasks and tool call dispatch
- Designed the `submit_to_municipality` tool schema with explicit `collected_data` properties so Gemini would correctly call the tool rather than handle submissions conversationally
- Debugged and fixed Gemini 1008 "policy violation" errors caused by oversized system instructions (reduced transcript injection from 40 → 10 messages)

**Error diagnosis and fixing**
All runtime errors were submitted directly to the coding agent for root-cause analysis and fix:

| Error | Root cause identified by agent | Fix applied |
|---|---|---|
| Gemini 1007 errors on session start | `additionalProperties: {type: string}` in tool schema rejected by Gemini Live API | Removed invalid schema field |
| Gemini 1008 session drops | System instruction too large (40-message transcript) + GoAway not acknowledged during tool call | Reduced transcript to last 10 messages |
| DU extraction `brotli` decode error | `aiohttp.ClientSession()` used without disabling brotli encoding | Added `Accept-Encoding: gzip, deflate` header to all sessions |
| `submit_to_municipality` never called | `collected_data` schema had `type: object` with no properties — Gemini refused to call the tool | Added 13 explicit field definitions to the schema |
| Supabase 401 RLS violations on writes | Publishable key blocked by Row Level Security on all three tables | Added `SUPABASE_SERVICE_KEY` (service role) for write operations |
| Citizen profile fields all NULL | DU returns `"First Name"` / `"Date of Birth"` (capitalised, spaced) but mapping expected `first_name` / `date_of_birth` | Rewrote `extracted_to_profile_fields()` to handle DU field names |

**Deployment**
- Wrote the Dockerfile and Azure startup script (`startup.sh`)
- Configured the GitHub Actions workflow for Azure App Service deployment
- Managed git commits, branch strategy, and `gh workflow run` triggers throughout the hackathon

### (c) How the agent output is integrated into the solution

The coding agent's output is not referenced or suggested — it is the running code. Every file listed in the Project Structure section above was either written or substantially modified by Claude Code during live debugging sessions. The agent:

- Read log files from the running server in real time to diagnose errors
- Applied fixes directly to source files using file-editing tools
- Restarted the server and verified fixes by re-running `grep` against the live log
- Committed and pushed changes to GitHub, then triggered Azure deployments via `gh` CLI

The entire integration pipeline — citizen speaks → Gemini extracts intent → Python tool called → document uploaded to UiPath bucket → Maestro job started → job polled → reply returned to citizen → data persisted to Supabase — was debugged end-to-end with Claude Code as the primary engineering tool.

## Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Google AI Studio API key |
| `GEMINI_VOICE` | No | Voice name (default: `Kore`) |
| `UIPATH_URL` | Yes | UiPath Orchestrator base URL |
| `UIPATH_ORGANIZATION` | Yes | Org slug |
| `UIPATH_TENANT` | Yes | Tenant slug |
| `UIPATH_CLIENT_ID` | Yes | OAuth2 client ID |
| `UIPATH_CLIENT_SECRET` | Yes | OAuth2 client secret |
| `UIPATH_FOLDER_NAME` | Yes | Orchestrator folder path |
| `UIPATH_PROCESS_KEY` | Yes | Maestro process key |
| `UIPATH_BUCKET_NAME` | Yes | Storage bucket name |
| `UIPATH_DU_PROJECT_ID` | Yes | Document Understanding project GUID |
| `SUPABASE_URL` | Yes | Supabase project URL |
| `SUPABASE_KEY` | Yes | Publishable key (reads) |
| `SUPABASE_SERVICE_KEY` | Yes | Service role key (writes) |
| `MICROSOFT_CLIENT_ID` | No | Microsoft OAuth2 app ID |
| `MICROSOFT_CLIENT_SECRET` | No | Microsoft OAuth2 secret |
| `GOOGLE_CLIENT_ID` | No | Google OAuth2 client ID |
| `GOOGLE_CLIENT_SECRET` | No | Google OAuth2 client secret |
| `SESSION_SECRET_KEY` | Yes | Cookie signing secret |
