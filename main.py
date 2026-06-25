import asyncio
import base64
import json
import logging
import os
import pathlib
import re
import uuid

from dotenv import load_dotenv
load_dotenv()  # Must run before any module that reads env vars at import time

from fastapi import Depends, FastAPI, File, Form, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from google import genai as google_genai
from google.genai import types as genai_types

from auth import require_auth, get_current_user, router as auth_router
from db import init_db, upsert_session, get_last_session
from gemini_live import GeminiLive
from supabase_db import (
    get_citizen_profile, upsert_citizen_profile,
    save_citizen_document, get_citizen_documents,
    save_citizen_session, get_latest_citizen_session,
    profile_to_citizen_data, extracted_to_profile_fields,
)
from twilio_handler import TwilioHandler
from uipath_fetch_id import extract_id_fields
from uipath_maestro import UiPathMaestroClient, UiPathMaestroConfig

def _setup_logging() -> logging.Logger:
    log_file = os.getenv("LOG_FILE")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.WARNING)
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        handlers.append(file_handler)
    logging.basicConfig(
        level=logging.INFO,
        handlers=handlers,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("gemini_live").setLevel(logging.INFO)
    _log = logging.getLogger(__name__)
    _log.setLevel(logging.INFO)
    if log_file:
        _log.info(f"File logging enabled → {log_file}")
    return _log

logger = _setup_logging()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("MODEL", "gemini-3.1-flash-live-preview")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_APP_HOST = os.getenv("TWILIO_APP_HOST")

app = FastAPI(title="Municipality Citizen AI Assistant")

# SessionMiddleware must wrap CORSMiddleware so cookies are available in all handlers
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET_KEY", "dev-secret-key-change-in-production"),
    https_only=os.getenv("SESSION_HTTPS_ONLY", "false").lower() == "true",
    same_site="lax",
)

app.include_router(auth_router)

app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.on_event("startup")
async def startup():
    await init_db()

# ─────────────────────────────────────────────────────────────────────────────
# Service requirements loader
# ─────────────────────────────────────────────────────────────────────────────

def _parse_service_requirements() -> dict[str, str]:
    """
    Parse service_requirements.md into a dict keyed by service name.
    Each value is the full markdown section for that service.
    Returns an empty dict if the file is missing.
    """
    req_file = pathlib.Path(__file__).parent / "service_requirements.md"
    if not req_file.exists():
        logger.warning("service_requirements.md not found — skipping data requirements injection")
        return {}

    text = req_file.read_text(encoding="utf-8")
    sections: dict[str, str] = {}

    # Split on level-2 headings (## ...) — each chunk is one service section
    parts = re.split(r"\n(?=## )", text)
    for part in parts:
        match = re.match(r"## (.+)", part.strip())
        if match:
            service_name = match.group(1).strip()
            sections[service_name] = part.strip()

    logger.info(f"Loaded service requirements for: {list(sections.keys())}")
    return sections


_SERVICE_REQUIREMENTS: dict[str, str] = _parse_service_requirements()


def _get_requirements(service: str) -> str:
    """Return the requirements section for the given service, or a generic note."""
    if not service or service == "General Inquiry":
        return ""
    # Exact match first, then case-insensitive prefix match
    if service in _SERVICE_REQUIREMENTS:
        return _SERVICE_REQUIREMENTS[service]
    for key in _SERVICE_REQUIREMENTS:
        if key.lower().startswith(service.lower().split()[0]):
            return _SERVICE_REQUIREMENTS[key]
    return ""


# Per-session store: session_id -> {"citizen_data": {}, "documents": [...]}
_sessions: dict = {}

_maestro = UiPathMaestroClient(UiPathMaestroConfig())

# Service-specific structured fields to include in in_CitizenData (default to "" if missing)
_ID_CARD_FIELDS = [
    "dateOfBirth", "placeOfBirth", "nationality", "address",
    "reasonForApplication", "currentIdNumber", "currentIdExpiry",
    "replacementReason", "policeReportReference",
]
_PASSPORT_FIELDS = [
    "dateOfBirth", "placeOfBirth", "nationality", "address",
    "reasonForApplication", "currentPassportNumber", "currentPassportExpiry",
    "plannedDepartureDate", "urgencyReason",
]

# Tool definition as a plain dict — widely compatible with google-genai SDK versions
_SUBMIT_TOOL = {
    "function_declarations": [
        {
            "name": "submit_to_municipality",
            "description": (
                "Submit the citizen's completed request to the municipal processing system. "
                "Call this ONLY when all required information has been collected and the citizen "
                "explicitly confirms they want to proceed with the submission. "
                "The system returns a reference number or status message for the citizen."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": (
                            "The service category being requested "
                            "(e.g. 'Passport Renewal', 'Construction Permit Application', 'ID Card Replacement')."
                        ),
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "Concise but complete summary of the citizen's specific request "
                            "and all key details collected during the conversation."
                        ),
                    },
                    "collected_data": {
                        "type": "object",
                        "description": "Structured citizen data collected during the conversation. Use empty string for missing fields.",
                        "properties": {
                            "dateOfBirth":          {"type": "string"},
                            "placeOfBirth":         {"type": "string"},
                            "nationality":          {"type": "string"},
                            "address":              {"type": "string"},
                            "reasonForApplication": {"type": "string"},
                            "currentIdNumber":      {"type": "string"},
                            "currentIdExpiry":      {"type": "string"},
                            "replacementReason":    {"type": "string"},
                            "policeReportReference":{"type": "string"},
                            "currentPassportNumber":{"type": "string"},
                            "currentPassportExpiry":{"type": "string"},
                            "plannedDepartureDate": {"type": "string"},
                            "urgencyReason":        {"type": "string"},
                        },
                    },
                },
                "required": ["subject", "summary"],
            },
        }
    ]
}


# ─────────────────────────────────────────────────────────────────────────────
# DU extraction notification builder
# ─────────────────────────────────────────────────────────────────────────────

_CITIZEN_DISPLAY_KEYS = {"name", "idNumber", "email", "phone"}

def _du_notification(fname: str, extracted: dict, citizen: dict) -> str:
    """
    Build the system message sent to Gemini after a successful DU extraction.
    Includes extracted fields and a comparison against existing registration data
    so the AI can flag and resolve any discrepancies with the citizen.
    """
    fields_str = "\n".join(f"  {k}: {v}" for k, v in extracted.items())

    known = {k: v for k, v in citizen.items() if v and k in _CITIZEN_DISPLAY_KEYS}
    if known:
        known_str = "\n".join(f"  {k}: {v}" for k, v in known.items())
        comparison = (
            f"\n\nThe citizen already provided these details at registration:\n{known_str}\n\n"
            "Compare the two sets of data carefully:\n"
            "- If a field from the document DIFFERS from the registration value, point out the "
            "discrepancy to the citizen and ask them to confirm which is correct. "
            "Do this for each mismatching field before moving on.\n"
            "- If everything matches, briefly confirm what you found and continue.\n"
            "- Use the confirmed (or matching) values for the rest of the conversation."
        )
    else:
        comparison = (
            "\n\nNo prior registration data to compare against. "
            "Confirm the extracted details with the citizen and use them going forward."
        )

    return (
        f"[System: Document Understanding extracted the following fields from '{fname}':\n"
        f"{fields_str}"
        f"{comparison}]"
    )


# ─────────────────────────────────────────────────────────────────────────────
# System instruction builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_system_instruction(citizen: dict, prev_transcript: list | None = None) -> str:
    name = citizen.get("name") or "Citizen"
    id_num = citizen.get("idNumber") or ""
    email = citizen.get("email") or ""
    phone = citizen.get("phone") or ""
    service = citizen.get("selectedService") or "General Inquiry"
    lang_code = citizen.get("preferredLanguage") or "en"

    # Build a list of fields that are already known so the AI never re-asks for them
    known_fields: list[str] = [f"Full name: {name}"]
    if id_num:
        known_fields.append(f"ID / Document number: {id_num}")
    if email:
        known_fields.append(f"Email address: {email}")
    if phone:
        known_fields.append(f"Phone number: {phone}")

    already_known_block = (
        "\n\nPRE-FILLED CITIZEN DATA (collected on the registration screen before this session):\n"
        + "\n".join(f"  - {f}" for f in known_fields)
        + "\n\nDo NOT ask the citizen for any of the above fields — they are already on record. "
        "Skip directly to any remaining missing information for the service checklist below."
    )

    # Inject pending UiPath job context so Gemini resumes intelligently
    pending_job = citizen.get("_pendingJob")
    if pending_job:
        already_known_block += (
            f"\n\nRESUMED APPLICATION — PENDING UIPATH JOB:\n"
            f"  The citizen has an existing application in progress.\n"
            f"  Service:     {pending_job.get('serviceType', 'Unknown')}\n"
            f"  Job key:     {pending_job.get('jobKey', 'Unknown')}\n"
            f"  Status:      {pending_job.get('jobStatus', 'submitted')}\n"
            f"  Submitted:   {pending_job.get('submittedAt', '')[:10]}\n\n"
            f"IMPORTANT: Do NOT start a new application. Greet the citizen by name, "
            f"tell them their previous {pending_job.get('serviceType', 'application')} "
            f"is still being processed (job key: {pending_job.get('jobKey', '')}), "
            f"and ask if they want a status update or have new information to add."
        )

    requirements_section = _get_requirements(service)
    requirements_block = (
        f"\n\n## Data Requirements for '{service}'\n\n"
        f"{requirements_section}\n\n"
        "Use the table above as your checklist. Work through the fields conversationally — "
        "do not read out the table literally. Ask one or two questions at a time to avoid "
        "overwhelming the citizen. If they cannot provide a field, note it as missing and continue.\n"
        "IMPORTANT: Even if some fields are missing, you MUST still call submit_to_municipality "
        "once the citizen confirms they want to proceed. The Maestro process handles incomplete "
        "data and will return a status such as 'OK', 'Data incomplete — pending review', or "
        "'Required data missing'. Relay that status clearly to the citizen."
    ) if requirements_section else ""

    return (
        "You are a professional and courteous AI assistant for the Municipality Citizen Services office. "
        "Your role is to assist citizens with inquiries and applications for:\n"
        "  - Identity Documents (ID cards): new applications, renewals, replacements\n"
        "  - Passports: new applications, renewals, emergency travel documents\n"
        "  - Work Permits: application requirements, procedures, document checklists\n"
        "  - Construction Permits: zoning inquiries, application procedures, inspection stages\n\n"
        "Communication guidelines:\n"
        "  - Be professional, patient, and empathetic.\n"
        "  - Use plain language — avoid bureaucratic jargon.\n"
        "  - Ask one or two questions at a time — never overwhelm the citizen with a long list.\n"
        "  - Address the citizen by first name when possible.\n"
        "  - Always read back a concise summary of collected data before submitting.\n\n"
        f"Current session information:\n"
        f"  Citizen name:      {name}\n"
        f"  Document number:   {id_num or 'not provided'}\n"
        f"  Email:             {email or 'not provided'}\n"
        f"  Phone:             {phone or 'not provided'}\n"
        f"  Requested service: {service}\n"
        f"  Preferred language: {lang_code}\n"
        f"{already_known_block}\n\n"
        f"Language: Your primary response language for this session is the one identified by "
        f"BCP-47 code '{lang_code}'. Always respond in that language. "
        "If the citizen writes or speaks in a different language during the conversation, "
        "switch to it naturally — always follow the language the citizen is actively using. "
        "You can also see the citizen's camera feed or a shared screen when they enable those features.\n\n"
        "Workflow:\n"
        "1. Greet the citizen warmly by name and confirm you can help with their selected service.\n"
        "2. Use the data requirements checklist below to guide the conversation — collect fields "
        "conversationally, not as a rigid form.\n"
        "3. If the citizen says they do not have a field, note it as 'not provided' and move on.\n"
        "4. When you have collected as much information as the citizen can provide, read back "
        "a brief summary and ask for confirmation to proceed.\n"
        "5. If documents were uploaded, acknowledge them by name and confirm their purpose.\n"
        "6. If the citizen shares their camera or screen, describe what you observe if relevant.\n"
        "7. On citizen confirmation, call submit_to_municipality. Do NOT wait for every field — "
        "partial data is acceptable; Maestro handles validation. "
        "Populate collected_data with ALL structured fields gathered in this conversation "
        "(dateOfBirth, placeOfBirth, nationality, address, reasonForApplication, etc.) — "
        "pass empty string for anything not provided, never omit a key.\n"
        "8. Relay the Maestro response (reference number, status, or next steps) clearly.\n\n"
        "Handle personal data with discretion. If you cannot answer a specific procedural "
        "question, direct the citizen to the relevant municipal department or advise an in-person visit."
        f"{requirements_block}"
        + _build_transcript_context(prev_transcript)
    )


def _build_transcript_context(transcript: list | None) -> str:
    if not transcript:
        return ""
    lines = []
    for msg in transcript[-10:]:  # last 10 messages to keep system instruction small
        role = "Citizen" if msg.get("type") == "user" else "Assistant"
        text = msg.get("text", "").strip()
        if text:
            lines.append(f"{role}: {text}")
    if not lines:
        return ""
    history = "\n".join(lines)
    return (
        "\n\n## Resumed Session — Previous Conversation\n\n"
        "The citizen is continuing a previous session. The conversation so far:\n\n"
        f"{history}\n\n"
        "Continue naturally from where the conversation left off. "
        "Do NOT re-introduce yourself or re-greet — pick up the context directly. "
        "Briefly acknowledge that you remember the previous discussion."
    )


# ─────────────────────────────────────────────────────────────────────────────
# REST Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse("frontend/index.html")


@app.get("/session/last")
async def session_last(request: Request, _user=Depends(require_auth)):
    """Return the most recent persisted session for the logged-in user."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"found": False})
    last = await get_last_session(user["sub"])
    if not last:
        return JSONResponse({"found": False})
    return JSONResponse({
        "found": True,
        "sessionId": last["session_id"],
        "citizenData": last["citizen_data"],
        "transcript": last["transcript"][-40:],  # last 40 messages for context
        "status": last["status"],
        "updatedAt": last["updated_at"],
    })


@app.post("/scan-id")
async def scan_id_card(request: Request, _user=Depends(require_auth)):
    """Extract citizen details from a photographed ID / Residence Permit card using Gemini Flash."""
    body = await request.json()
    image_b64 = body.get("image", "")
    if not image_b64:
        return JSONResponse({"success": False, "error": "No image provided"}, status_code=400)

    try:
        image_bytes = base64.b64decode(image_b64)
    except Exception:
        return JSONResponse({"success": False, "error": "Invalid image data"}, status_code=400)

    try:
        client = google_genai.Client(api_key=GEMINI_API_KEY)
        response = await client.aio.models.generate_content(
            model="gemini-2.0-flash",
            contents=[
                genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                genai_types.Part.from_text(text=(
                    "You are an ID card OCR engine. Extract all readable information from this "
                    "identity document / residence permit / RP card image.\n"
                    "Return ONLY a valid JSON object with these fields (null if not visible):\n"
                    "{\n"
                    '  "name": "full name as printed",\n'
                    '  "id_number": "document/ID/permit number",\n'
                    '  "date_of_birth": "DD/MM/YYYY or as printed",\n'
                    '  "nationality": "country name",\n'
                    '  "expiry_date": "DD/MM/YYYY or as printed",\n'
                    '  "address": "address if visible",\n'
                    '  "document_type": "type e.g. Residence Permit / Passport / ID Card",\n'
                    '  "gender": "M or F or as printed",\n'
                    '  "place_of_birth": "city/country if visible"\n'
                    "}\n"
                    "Return ONLY the JSON. No explanation, no markdown, no code fences."
                )),
            ],
        )
        raw = response.text.strip()
        logger.info(f"ID scan raw Gemini response: {raw[:300]}")
        # Strip markdown code fences if model wraps in them
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        raw = raw.strip()
        data = json.loads(raw)
        logger.info(f"ID scan extracted fields: {list(k for k,v in data.items() if v)}")
        return JSONResponse({"success": True, "data": data})
    except json.JSONDecodeError:
        return JSONResponse({"success": False, "error": "Could not parse card details — try a clearer photo"})
    except Exception as exc:
        logger.error(f"scan-id error: {exc}")
        return JSONResponse({"success": False, "error": str(exc)}, status_code=500)


@app.get("/api/citizen-profile")
async def citizen_profile_get(request: Request, _user=Depends(require_auth)):
    """Return saved Supabase profile + document history for the logged-in citizen."""
    user = get_current_user(request)
    email = (user or {}).get("email", "")
    profile = await get_citizen_profile(email)
    documents = await get_citizen_documents(email)
    if not profile:
        return JSONResponse({"found": False, "documents": documents})
    return JSONResponse({
        "found": True,
        "citizenData": profile_to_citizen_data(profile),
        "documents": documents,
        "profile": profile,
    })


@app.post("/api/citizen-profile")
async def citizen_profile_save(request: Request, data: dict, _user=Depends(require_auth)):
    """Save or update citizen profile in Supabase."""
    user = get_current_user(request)
    email = (user or {}).get("email", "") or data.get("email", "")
    if not email:
        return JSONResponse({"status": "error", "detail": "No email"}, status_code=400)
    fields = {
        "full_name":     data.get("name") or data.get("full_name"),
        "id_number":     data.get("id_number"),
        "phone":         data.get("phone"),
        "nationality":   data.get("nationality"),
        "date_of_birth": data.get("date_of_birth"),
        "address":       data.get("address"),
        "gender":        data.get("gender"),
    }
    ok = await upsert_citizen_profile(email, fields)
    return JSONResponse({"status": "ok" if ok else "error"})


@app.post("/session/init")
async def session_init(request: Request, data: dict, _user=Depends(require_auth)):
    """Create or refresh a citizen session with profile data before the WebSocket connects."""
    session_id = data.get("sessionId") or str(uuid.uuid4())
    existing_docs = _sessions.get(session_id, {}).get("documents", [])

    citizen_data: dict = data.get("citizenData", {})

    # Merge Supabase profile + previous UiPath job state
    email = citizen_data.get("email", "") or (get_current_user(request) or {}).get("email", "")
    pending_job: dict | None = None
    if email:
        # 1. Merge saved profile fields (fill blanks only)
        profile = await get_citizen_profile(email)
        if profile:
            saved = profile_to_citizen_data(profile)
            for k, v in saved.items():
                if v and not citizen_data.get(k):
                    citizen_data[k] = v
            logger.info(f"Supabase profile merged for {email}")

        # 2. Load previous UiPath job so Gemini can resume the application
        prev_session = await get_latest_citizen_session(email)
        if prev_session and prev_session.get("job_status") not in ("completed", "cancelled"):
            pending_job = {
                "jobKey":      prev_session.get("uipath_job_key"),
                "jobId":       prev_session.get("uipath_job_id"),
                "serviceType": prev_session.get("service_type"),
                "jobStatus":   prev_session.get("job_status"),
                "submittedAt": prev_session.get("updated_at"),
            }
            citizen_data["_pendingJob"] = pending_job
            logger.info(f"Previous UiPath job loaded for {email}: {pending_job['jobKey']}")

    _sessions[session_id] = {
        "citizen_data": citizen_data,
        "documents": existing_docs,
        "transcript": data.get("transcript", []),
    }
    logger.info(f"Session initialised: {session_id[:8]}…")
    return {
        "sessionId":   session_id,
        "status":      "ok",
        "profileLoaded": bool(email),
        "pendingJob":  pending_job,
    }


@app.post("/upload-document")
async def upload_document(
    sessionId: str = Form(...),
    file: UploadFile = File(...),
    _user=Depends(require_auth),
):
    """Attach an uploaded document to an active citizen session."""
    if sessionId not in _sessions:
        _sessions[sessionId] = {"citizen_data": {}, "documents": []}

    MAX_BYTES = 10 * 1024 * 1024  # 10 MB hard limit
    content = await file.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        return JSONResponse(status_code=413, content={"error": "File exceeds the 10 MB limit."})

    _sessions[sessionId]["documents"].append({
        "filename": file.filename,
        "content_type": file.content_type or "application/octet-stream",
        "data": content,
    })
    logger.info(f"Document saved: session={sessionId[:8]}, file={file.filename}, size={len(content)}")

    # Attempt automatic ID field extraction via Document Understanding
    extracted: dict = {}
    try:
        result = await extract_id_fields(content, file.filename)
        if result:
            extracted = result
            _sessions[sessionId].setdefault("du_extracted", {}).update(extracted)
            logger.info(f"DU extracted {list(extracted.keys())} from '{file.filename}' session={sessionId[:8]}")
    except Exception as exc:
        logger.warning(f"DU extraction skipped for '{file.filename}': {exc}")

    # Persist document + extracted fields to Supabase (best-effort)
    citizen_email = _sessions[sessionId].get("citizen_data", {}).get("email", "")
    if citizen_email:
        doc_type = _sessions[sessionId].get("citizen_data", {}).get("service", "identity_document")
        asyncio.create_task(save_citizen_document(
            email=citizen_email,
            filename=file.filename,
            document_type=doc_type,
            extracted_fields=extracted,
        ))
        if extracted:
            asyncio.create_task(upsert_citizen_profile(
                email=citizen_email,
                fields=extracted_to_profile_fields(extracted),
            ))

    return {"status": "ok", "filename": file.filename, "size": len(content), "extracted": extracted}


@app.delete("/upload-document")
async def delete_document(sessionId: str, filename: str):
    """Remove a specific document from a session."""
    if sessionId in _sessions:
        _sessions[sessionId]["documents"] = [
            d for d in _sessions[sessionId]["documents"]
            if d["filename"] != filename
        ]
    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket — Main AI session
# ─────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    session_id: str = Query(default=None),
):
    """Primary WebSocket endpoint for the Municipality AI Assistant."""
    await websocket.accept()
    logger.info(f"WebSocket accepted: session={session_id}")

    # Enforce auth on the WebSocket handshake when OAuth is configured
    if not get_current_user(websocket):
        from auth import _oauth_enabled
        if _oauth_enabled():
            await websocket.close(code=1008, reason="Unauthorized")
            return

    if not session_id or session_id not in _sessions:
        session_id = session_id or str(uuid.uuid4())
        _sessions[session_id] = {"citizen_data": {}, "documents": [], "transcript": []}

    session_data = _sessions[session_id]
    citizen_data = session_data.get("citizen_data", {})

    # Inject previous transcript so Gemini can continue the conversation
    prev_transcript = session_data.get("transcript", [])
    system_instruction = _build_system_instruction(citizen_data, prev_transcript or None)

    # Pre-fetch UiPath token + folder + release key in the background
    # so they are already cached when submit_to_municipality is called
    asyncio.create_task(_maestro.warmup())

    # Pre-fetch UiPath token + folder + release key in the background
    # so they are already cached when submit_to_municipality is called
    asyncio.create_task(_maestro.warmup())

    audio_q: asyncio.Queue = asyncio.Queue()
    video_q: asyncio.Queue = asyncio.Queue()
    text_q: asyncio.Queue = asyncio.Queue()
    # Lock that serialises all WebSocket writes — prevents the concurrent-send
    # AssertionError that occurs when on_audio_out (called from receive_loop task)
    # and the main event loop both try to write the socket at the same time.
    ws_send_lock = asyncio.Lock()

    async def on_audio_out(data: bytes) -> None:
        try:
            async with ws_send_lock:
                await websocket.send_bytes(data)
        except Exception:
            pass  # client disconnected

    async def handle_submit(
        subject: str,
        summary: str,
        collected_data: dict | None = None,
    ) -> str:
        """Tool handler: forwards citizen request to UiPath Maestro."""
        logger.warning(f"[handle_submit] called: subject={subject!r} docs={len(session_data.get('documents',[]))} collected_data_type={type(collected_data).__name__}")
        # Flush any pending AI turn into the chat log before capturing it
        if session_data.get("_ai_turn"):
            ai_text = "".join(session_data.pop("_ai_turn"))
            session_data.setdefault("chat_log", []).append(f"Agent: {ai_text}")
        in_chat = "\n".join(session_data.get("chat_log", []))

        docs: list[dict] = [
            {"name": d["filename"], "data": d["data"]}
            for d in session_data.get("documents", [])
        ]
        for img in session_data.get("captured_images", []):
            try:
                docs.append({
                    "name": img["filename"],
                    "data": base64.b64decode(img["data_b64"]),
                })
            except Exception as exc:
                logger.warning(f"Could not decode captured image '{img.get('filename')}': {exc}")

        # Build in_CitizenData: registration data → DU extracted fields → AI-collected fields
        du_fields = session_data.get("du_extracted", {})
        # collected_data may arrive as a protobuf MapComposite — convert to plain dict
        try:
            cd = dict(collected_data) if collected_data else {}
        except Exception:
            cd = {}
        enriched: dict = {
            **citizen_data,
            **{k: v for k, v in du_fields.items() if v},   # DU raw fields
            **{k: v for k, v in cd.items() if v},           # AI-collected structured fields
            "requestSummary": summary,
            "capturedPhotoCount": len(session_data.get("captured_images", [])),
            "documentCount": len(docs),
        }
        # Ensure all service-specific fields exist (empty string if missing)
        service = citizen_data.get("selectedService", "")
        if "identity" in service.lower() or "id card" in service.lower():
            for f in _ID_CARD_FIELDS:
                enriched.setdefault(f, "")
        elif "passport" in service.lower():
            for f in _PASSPORT_FIELDS:
                enriched.setdefault(f, "")

        result = await _maestro.submit(
            session_id=session_id,
            subject=subject,
            citizen_data=enriched,
            documents=docs or None,
            chat=in_chat,
        )
        logger.warning(f"[handle_submit] maestro result: {result[:120]!r}")

        # Persist job to Supabase so citizen can resume next visit
        email = citizen_data.get("email", "")
        if email:
            # Extract job ID/key from result string (format: "Job key: xxx")
            import re as _re
            job_key = (_re.search(r"[Jj]ob\s+key[:\s]+(\S+)", result or "") or
                       _re.search(r"([a-f0-9\-]{8,})", result or ""))
            job_key = job_key.group(1) if job_key else session_id
            asyncio.create_task(save_citizen_session(
                email=email,
                session_id=session_id,
                uipath_job_id=session_id,
                uipath_job_key=job_key,
                service_type=citizen_data.get("selectedService", subject),
                citizen_data=enriched,
                transcript=session_data.get("transcript", []),
                job_status="submitted",
            ))
            logger.info(f"[Supabase] UiPath job cached for {email} — key: {job_key}")

        return result

    gemini = GeminiLive(
        api_key=GEMINI_API_KEY,
        model=MODEL,
        input_sample_rate=16000,
        system_instruction=system_instruction,
        tools=[_SUBMIT_TOOL],
        tool_mapping={"submit_to_municipality": handle_submit},
    )

    async def receive_from_client() -> None:
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("bytes"):
                    await audio_q.put(msg["bytes"])
                elif msg.get("text"):
                    try:
                        payload = json.loads(msg["text"])
                        if isinstance(payload, dict):
                            if payload.get("type") == "image":
                                await video_q.put(base64.b64decode(payload["data"]))
                                continue
                            if payload.get("type") == "document_notify":
                                fname = payload.get("filename", "a document")
                                extracted = payload.get("extracted", {})
                                if extracted:
                                    await text_q.put(
                                        _du_notification(fname, extracted, citizen_data)
                                    )
                                else:
                                    await text_q.put(
                                        f"[System: The citizen has just uploaded a document: {fname}. "
                                        f"Acknowledge its receipt in the conversation.]"
                                    )
                                continue
                            if payload.get("type") == "capture_photo":
                                fname = payload.get("filename") or f"photo_{uuid.uuid4().hex[:8]}.jpg"
                                b64 = payload.get("data", "")
                                session_data.setdefault("captured_images", []).append(
                                    {"filename": fname, "data_b64": b64}
                                )
                                logger.info(
                                    f"Photo captured: session={session_id[:8]}, "
                                    f"file={fname}, size_b64={len(b64)}"
                                )
                                total = len(session_data["captured_images"])

                                # Run DU extraction in background; result is pushed back
                                # to the frontend as a du_result WebSocket event
                                async def _du_photo(fname=fname, b64=b64) -> None:
                                    extracted: dict = {}
                                    try:
                                        img_bytes = base64.b64decode(b64)
                                        result = await extract_id_fields(img_bytes, fname)
                                        extracted = result or {}
                                    except Exception as exc:
                                        logger.warning(f"DU photo extraction failed for '{fname}': {exc}")
                                    # Push UI update
                                    try:
                                        async with ws_send_lock:
                                            await websocket.send_json(
                                                {"type": "du_result", "filename": fname, "extracted": extracted}
                                            )
                                    except Exception:
                                        pass
                                    # Persist extracted fields in session and notify AI
                                    if extracted:
                                        session_data.setdefault("du_extracted", {}).update(extracted)
                                        await text_q.put(
                                            _du_notification(fname, extracted, citizen_data)
                                        )

                                asyncio.create_task(_du_photo())

                                await text_q.put(
                                    f"[System: The citizen has captured a photo from their camera: '{fname}'. "
                                    f"Total captured photos this session: {total}. "
                                    f"This photo will be included automatically when the request is submitted. "
                                    f"Acknowledge the capture briefly — e.g. confirm it will be used as their "
                                    f"passport/ID photo if that is the relevant service.]"
                                )
                                continue
                    except (json.JSONDecodeError, KeyError):
                        pass
                    await text_q.put(msg["text"])
        except WebSocketDisconnect:
            logger.info(f"WebSocket disconnected: session={session_id}")
        except Exception as exc:
            logger.error(f"receive_from_client error: {exc}")

    receive_task = asyncio.create_task(receive_from_client())

    # Live transcript collected during this session
    live_transcript: list[dict] = list(prev_transcript)

    try:
        async for event in gemini.start_session(
            audio_input_queue=audio_q,
            video_input_queue=video_q,
            text_input_queue=text_q,
            audio_output_callback=on_audio_out,
        ):
            if event:
                # Accumulate conversation for in_Chat
                etype = event.get("type", "")
                etext = event.get("text", "")
                if etype == "gemini" and etext:
                    session_data.setdefault("_ai_turn", []).append(etext)
                elif etext:
                    # Non-gemini text event: flush completed AI turn first
                    if session_data.get("_ai_turn"):
                        ai_text = "".join(session_data.pop("_ai_turn"))
                        session_data.setdefault("chat_log", []).append(f"Agent: {ai_text}")
                    if etype == "input_transcript":
                        session_data.setdefault("chat_log", []).append(f"User: {etext}")
                try:
                    async with ws_send_lock:
                        await websocket.send_json(event)
                    # Accumulate text turns for persistence
                    if isinstance(event, dict) and event.get("type") in ("user", "gemini"):
                        live_transcript.append({
                            "type": event["type"],
                            "text": event.get("text", ""),
                        })
                except Exception as exc:
                    logger.info(
                        f"WebSocket send failed (client disconnected): "
                        f"{type(exc).__name__} — session={session_id[:8]}"
                    )
                    break
    except Exception as exc:
        import traceback
        logger.error(f"Gemini session error: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
    finally:
        receive_task.cancel()
        # Persist session to DB so the user can resume later
        logged_in_user = get_current_user(websocket)
        if logged_in_user and live_transcript:
            try:
                await upsert_session(
                    session_id=session_id,
                    user_sub=logged_in_user["sub"],
                    user_email=logged_in_user.get("email", ""),
                    user_name=logged_in_user.get("name", ""),
                    citizen_data=citizen_data,
                    transcript=live_transcript,
                )
                logger.info(f"Session persisted: {session_id[:8]}… ({len(live_transcript)} messages)")
            except Exception as exc:
                logger.error(f"Failed to persist session: {exc}")
        try:
            await websocket.close()
        except Exception:
            pass
        _sessions.pop(session_id, None)
        logger.info(f"Session cleaned up: {session_id}")


# ─────────────────────────────────────────────────────────────────────────────
# Twilio Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/twilio/inbound")
async def twilio_inbound():
    host = TWILIO_APP_HOST or "localhost:8000"
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Connecting to the municipal AI assistant.</Say>
    <Connect>
        <Stream url="wss://{host}/twilio/stream" />
    </Connect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.post("/twilio/outbound")
async def twilio_outbound(
    to_number: str = Query(..., description="Destination phone number (E.164 format)"),
    from_number: str = Query(..., description="Your Twilio phone number (E.164 format)"),
):
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return {"error": "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set."}
    if not TWILIO_APP_HOST:
        return {"error": "TWILIO_APP_HOST must be set."}

    from twilio.rest import Client as TwilioClient
    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    twiml = f"""<Response>
    <Say>Connecting to the municipal AI assistant.</Say>
    <Connect>
        <Stream url="wss://{TWILIO_APP_HOST}/twilio/stream" />
    </Connect>
</Response>"""
    call = client.calls.create(to=to_number, from_=from_number, twiml=twiml)
    logger.info(f"Outbound call initiated: {call.sid}")
    return {"callSid": call.sid, "status": call.status}


@app.websocket("/twilio/stream")
async def twilio_stream(websocket: WebSocket):
    await websocket.accept()
    logger.info("Twilio media stream connected")
    handler = TwilioHandler(gemini_api_key=GEMINI_API_KEY, model=MODEL)
    try:
        await handler.handle_media_stream(websocket)
    except Exception as exc:
        logger.error(f"Twilio stream error: {exc}", exc_info=True)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("Twilio media stream closed")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
