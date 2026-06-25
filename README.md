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
