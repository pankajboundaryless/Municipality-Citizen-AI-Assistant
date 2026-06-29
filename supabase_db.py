"""
Supabase integration for citizen profiles and document history.
Uses httpx (already in requirements) to call the Supabase REST API directly.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
# Service role key bypasses RLS — required for server-side writes
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "") or SUPABASE_KEY


def _headers(write: bool = False) -> dict:
    key = SUPABASE_SERVICE_KEY if write else SUPABASE_KEY
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _is_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


# ── Citizen Profile ──────────────────────────────────────────────────────────

async def get_citizen_profile(email: str) -> dict | None:
    """Return saved profile for this email, or None if not found."""
    if not _is_configured() or not email:
        return None
    url = f"{SUPABASE_URL}/rest/v1/citizen_profiles?user_email=eq.{email}&select=*&limit=1"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=_headers(), timeout=8)
        if r.status_code == 200 and r.json():
            return r.json()[0]
    except Exception as exc:
        logger.warning(f"[Supabase] get_citizen_profile failed: {exc}")
    return None


async def upsert_citizen_profile(email: str, fields: dict) -> bool:
    """Create or update the citizen profile for this email."""
    if not _is_configured() or not email:
        return False
    payload = {
        "user_email": email,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **{k: v for k, v in fields.items() if v},
    }
    url = f"{SUPABASE_URL}/rest/v1/citizen_profiles?on_conflict=user_email"
    headers = {**_headers(write=True), "Prefer": "resolution=merge-duplicates,return=representation"}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, headers=headers, content=json.dumps(payload), timeout=8)
        if r.status_code in (200, 201):
            logger.info(f"[Supabase] Profile upserted for {email}")
            return True
        logger.warning(f"[Supabase] upsert_citizen_profile HTTP {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        logger.warning(f"[Supabase] upsert_citizen_profile failed: {exc}")
    return False


# ── Citizen Documents ────────────────────────────────────────────────────────

async def save_citizen_document(
    email: str,
    filename: str,
    document_type: str,
    extracted_fields: dict,
) -> bool:
    """Save a document upload record with DU-extracted fields."""
    if not _is_configured() or not email:
        return False
    payload = {
        "user_email": email,
        "filename": filename,
        "document_type": document_type,
        "extracted_fields": extracted_fields,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    url = f"{SUPABASE_URL}/rest/v1/citizen_documents"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, headers=_headers(write=True), content=json.dumps(payload), timeout=8)
        if r.status_code in (200, 201):
            logger.info(f"[Supabase] Document saved: {filename} for {email}")
            return True
        logger.warning(f"[Supabase] save_citizen_document HTTP {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        logger.warning(f"[Supabase] save_citizen_document failed: {exc}")
    return False


async def get_citizen_documents(email: str) -> list[dict]:
    """Return all uploaded documents for this citizen, newest first."""
    if not _is_configured() or not email:
        return []
    url = (
        f"{SUPABASE_URL}/rest/v1/citizen_documents"
        f"?user_email=eq.{email}&select=*&order=uploaded_at.desc"
    )
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=_headers(), timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception as exc:
        logger.warning(f"[Supabase] get_citizen_documents failed: {exc}")
    return []


# ── Citizen Sessions (UiPath job cache) ─────────────────────────────────────

async def save_citizen_session(
    email: str,
    session_id: str,
    uipath_job_id: str,
    uipath_job_key: str,
    service_type: str,
    citizen_data: dict,
    transcript: list,
    job_status: str = "pending",
) -> bool:
    """Upsert the citizen's latest UiPath job + session state into Supabase."""
    if not _is_configured() or not email:
        return False
    payload = {
        "user_email":      email,
        "session_id":      session_id,
        "uipath_job_id":   uipath_job_id,
        "uipath_job_key":  uipath_job_key,
        "service_type":    service_type,
        "job_status":      job_status,
        "citizen_data":    citizen_data,
        "transcript":      transcript[-30:],  # keep last 30 messages
        "updated_at":      datetime.now(timezone.utc).isoformat(),
    }
    url = f"{SUPABASE_URL}/rest/v1/citizen_sessions?on_conflict=user_email"
    headers = {**_headers(write=True), "Prefer": "resolution=merge-duplicates,return=representation"}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, headers=headers, content=json.dumps(payload), timeout=8)
        if r.status_code in (200, 201):
            logger.info(f"[Supabase] Session saved for {email} — job: {uipath_job_id}")
            return True
        logger.warning(f"[Supabase] save_citizen_session HTTP {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        logger.warning(f"[Supabase] save_citizen_session failed: {exc}")
    return False


async def get_latest_citizen_session(email: str) -> dict | None:
    """Return the most recent session with pending/running UiPath job, or None."""
    if not _is_configured() or not email:
        return None
    url = (
        f"{SUPABASE_URL}/rest/v1/citizen_sessions"
        f"?user_email=eq.{email}&order=updated_at.desc&limit=1&select=*"
    )
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=_headers(), timeout=8)
        if r.status_code == 200 and r.json():
            return r.json()[0]
    except Exception as exc:
        logger.warning(f"[Supabase] get_latest_citizen_session failed: {exc}")
    return None


async def update_session_job_status(email: str, job_status: str) -> bool:
    """Update the job_status for the citizen's latest session."""
    if not _is_configured() or not email:
        return False
    payload = {"job_status": job_status, "updated_at": datetime.now(timezone.utc).isoformat()}
    url = f"{SUPABASE_URL}/rest/v1/citizen_sessions?user_email=eq.{email}&order=updated_at.desc&limit=1"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.patch(url, headers=_headers(write=True), content=json.dumps(payload), timeout=8)
        return r.status_code in (200, 204)
    except Exception as exc:
        logger.warning(f"[Supabase] update_session_job_status failed: {exc}")
    return False


# ── Helpers ──────────────────────────────────────────────────────────────────

def profile_to_citizen_data(profile: dict) -> dict:
    """Map Supabase profile columns → citizenData keys used by main.py."""
    return {
        "name":          profile.get("full_name") or "",
        "id_number":     profile.get("id_number") or "",
        "email":         profile.get("user_email") or "",
        "phone":         profile.get("phone") or "",
        "nationality":   profile.get("nationality") or "",
        "date_of_birth": profile.get("date_of_birth") or "",
        "address":       profile.get("address") or "",
        "gender":        profile.get("gender") or "",
    }


def extracted_to_profile_fields(extracted: dict) -> dict:
    """Map DU-extracted field names → citizen_profiles column names.

    DU returns capitalised spaced names e.g. 'First Name', 'Date of Birth'.
    We also handle lowercase/underscore variants for fallback.
    """
    result: dict[str, Any] = {"raw_extracted": extracted}

    # Build full_name from First Name + Last Name (DU splits them)
    first = (extracted.get("First Name") or extracted.get("first_name") or "").strip()
    last  = (extracted.get("Last Name")  or extracted.get("last_name")  or "").strip()
    if first or last:
        result["full_name"] = f"{first} {last}".strip()
    elif extracted.get("name") or extracted.get("full_name"):
        result["full_name"] = extracted.get("name") or extracted.get("full_name")

    # Direct field mapping — DU names first, lowercase fallbacks second
    mapping = {
        "Passport Number":   "id_number",
        "Document Number":   "id_number",
        "id_number":         "id_number",
        "document_number":   "id_number",
        "Nationality":       "nationality",
        "nationality":       "nationality",
        "Date of Birth":     "date_of_birth",
        "date_of_birth":     "date_of_birth",
        "dob":               "date_of_birth",
        "Gender":            "gender",
        "gender":            "gender",
        "Place of Birth":    "place_of_birth",
        "place_of_birth":    "place_of_birth",
        "Expiry Date":       "expiry_date",
        "Date of Expiry":    "expiry_date",
        "expiry_date":       "expiry_date",
        "address":           "address",
    }
    for src_key, db_col in mapping.items():
        if db_col in result:
            continue  # already set by a higher-priority key
        val = extracted.get(src_key)
        if val:
            result[db_col] = str(val)
    return result
