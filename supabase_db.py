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


def _headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
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
    headers = {**_headers(), "Prefer": "resolution=merge-duplicates,return=representation"}
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
            r = await client.post(url, headers=_headers(), content=json.dumps(payload), timeout=8)
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
    """Map DU/Gemini extracted field names → citizen_profiles column names."""
    mapping = {
        "name":           "full_name",
        "full_name":      "full_name",
        "id_number":      "id_number",
        "document_number":"id_number",
        "nationality":    "nationality",
        "date_of_birth":  "date_of_birth",
        "dob":            "date_of_birth",
        "address":        "address",
        "gender":         "gender",
        "place_of_birth": "place_of_birth",
        "expiry_date":    "expiry_date",
    }
    result: dict[str, Any] = {"raw_extracted": extracted}
    for src_key, db_col in mapping.items():
        val = extracted.get(src_key)
        if val:
            result[db_col] = str(val)
    return result
