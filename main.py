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
from twilio_handler import TwilioHandler
from uipath_maestro import UiPathMaestroClient, UiPathMaestroConfig

def _setup_logging() -> logging.Logger:
    log_file = os.getenv("LOG_FILE")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
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
    logging.getLogger("gemini_live").setLevel(logging.DEBUG)
    _log = logging.getLogger(__name__)
    _log.setLevel(logging.DEBUG)
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
                },
                "required": ["subject", "summary"],
            },
        }
    ]
}


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
        "partial data is acceptable; Maestro handles validation.\n"
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
    for msg in transcript[-40:]:
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


@app.post("/session/init")
async def session_init(data: dict, _user=Depends(require_auth)):
    """Create or refresh a citizen session with profile data before the WebSocket connects."""
    session_id = data.get("sessionId") or str(uuid.uuid4())
    existing_docs = _sessions.get(session_id, {}).get("documents", [])
    _sessions[session_id] = {
        "citizen_data": data.get("citizenData", {}),
        "documents": existing_docs,
        "transcript": data.get("transcript", []),  # carry over previous transcript if resuming
    }
    logger.info(f"Session initialised: {session_id[:8]}…")
    return {"sessionId": session_id, "status": "ok"}


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
    return {"status": "ok", "filename": file.filename, "size": len(content)}


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

    async def handle_submit(subject: str, summary: str) -> str:
        """Tool handler: forwards citizen request to UiPath Maestro."""
        # Uploaded documents (raw bytes)
        docs: list[bytes] = [d["data"] for d in session_data.get("documents", [])]
        # Captured camera photos (base64 → bytes)
        for img in session_data.get("captured_images", []):
            try:
                docs.append(base64.b64decode(img["data_b64"]))
            except Exception as exc:
                logger.warning(f"Could not decode captured image '{img.get('filename')}': {exc}")
        enriched = {
            **citizen_data,
            "requestSummary": summary,
            "capturedPhotoCount": len(session_data.get("captured_images", [])),
        }
        return await _maestro.submit(
            session_id=session_id,
            subject=subject,
            citizen_data=enriched,
            documents=docs or None,
        )

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
