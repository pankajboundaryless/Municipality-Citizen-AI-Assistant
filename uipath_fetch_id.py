"""
UiPath Document Understanding integration for automatic ID document extraction.

When a citizen uploads a photo ID, this module digitizes it via the UiPath
Document Understanding API, extracts the fields, and returns a dict of
{fieldName: value} pairs as returned by the extractor (no manual mapping).

Required env vars (shared with uipath_maestro.py):
  UIPATH_URL            — base URL (default: https://staging.uipath.com)
  UIPATH_ORGANIZATION   — organization slug
  UIPATH_TENANT         — tenant slug
  UIPATH_CLIENT_ID      — OAuth2 client ID
  UIPATH_CLIENT_SECRET  — OAuth2 client secret

New env var:
  UIPATH_DU_PROJECT_ID  — Document Understanding project GUID
"""

import asyncio
import hashlib
import json
import logging
import os
import pathlib
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

# Env vars are read at call time (not module import time) so that load_dotenv()
# in main.py takes effect before these values are used.

# ─────────────────────────────────────────────────────────────────────────────
# Extraction cache — keyed by SHA-256 of the file bytes, persisted to disk so
# re-submitting the same document (e.g. same ID photo) skips DU entirely and
# reuses the previous extraction.
# ─────────────────────────────────────────────────────────────────────────────

_CACHE_FILE = pathlib.Path(__file__).parent / "du_extraction_cache.json"
_cache: Dict[str, Dict[str, Any]] = {}
_cache_loaded = False


def _load_cache() -> None:
    global _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    if _CACHE_FILE.exists():
        try:
            _cache.update(json.loads(_CACHE_FILE.read_text(encoding="utf-8")))
            logger.info(f"Loaded {len(_cache)} cached DU extraction(s) from {_CACHE_FILE.name}")
        except Exception as exc:
            logger.warning(f"Failed to load DU extraction cache: {exc}")


def _save_cache() -> None:
    try:
        _CACHE_FILE.write_text(json.dumps(_cache, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"Failed to save DU extraction cache: {exc}")


def _hash_bytes(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()


def _cfg() -> tuple:
    """Return (base_url, org, tenant, client_id, client_secret, du_project_id)."""
    return (
        os.getenv("UIPATH_URL", "https://staging.uipath.com").rstrip("/"),
        os.getenv("UIPATH_ORGANIZATION", ""),
        os.getenv("UIPATH_TENANT", ""),
        os.getenv("UIPATH_CLIENT_ID", ""),
        os.getenv("UIPATH_CLIENT_SECRET", ""),
        os.getenv("UIPATH_DU_PROJECT_ID", ""),
    )


def _is_configured() -> bool:
    _, org, tenant, cid, csec, project = _cfg()
    return bool(org and tenant and cid and csec and project)


def _du_base() -> str:
    base, org, tenant, _, _, project = _cfg()
    return f"{base}/{org}/{tenant}/du_/api/framework/projects/{project}/"


async def _get_token() -> str:
    base, _, _, client_id, client_secret, _ = _cfg()
    url = f"{base}/identity_/connect/token"
    payload = {
        "grant_type":    "client_credentials",
        "client_id":     client_id,
        "client_secret": client_secret,
        "scope":         "Du.Digitization.Api Du.Extraction.Api Du.DocumentManager.Document Du.Classification.Api Du.Validation.Api Du.DataDeletion.Api",
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, data=payload) as r:
            if r.status != 200:
                raise RuntimeError(f"DU auth HTTP {r.status}: {await r.text()}")
            data = await r.json()
    return data["access_token"]


async def _digitize(file_bytes: bytes, filename: str, token: str) -> str:
    url = f"{_du_base()}digitization/start?api-version=1"
    headers = {"Authorization": f"Bearer {token}"}
    form = aiohttp.FormData()
    form.add_field("file", file_bytes, filename=filename, content_type="multipart/form-data")
    async with aiohttp.ClientSession() as s:
        async with s.post(url, data=form, headers=headers) as r:
            if r.status not in (200, 201, 202):
                raise RuntimeError(f"Digitize HTTP {r.status}: {await r.text()}")
            body = await r.json()

    document_id: str = body["documentId"]
    result_url: str = body.get("resultUrl", "")

    if not result_url:
        return document_id

    # 202 means processing started — poll resultUrl until digitization completes
    poll_headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(30):
        await asyncio.sleep(2)
        async with aiohttp.ClientSession() as s:
            async with s.get(result_url, headers=poll_headers) as r:
                if r.status == 200:
                    logger.info(f"Digitization complete after {attempt + 1} poll(s)")
                    return document_id
                if r.status not in (202, 404):
                    raise RuntimeError(f"Digitize poll HTTP {r.status}: {await r.text()}")
        logger.debug(f"Digitization poll {attempt + 1}: still processing…")

    raise RuntimeError(f"Digitization timed out for document {document_id}")


async def _get_extractors(token: str) -> List[Dict[str, Any]]:
    url = f"{_du_base()}extractors/?api-version=1"
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers) as r:
            if r.status != 200:
                raise RuntimeError(f"Extractors HTTP {r.status}: {await r.text()}")
            return (await r.json()).get("extractors", [])


async def _extract(extractor_id: str, document_id: str, token: str) -> Dict[str, Any]:
    url = f"{_du_base()}extractors/{extractor_id}/extraction?api-version=1"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json={"documentId": document_id}, headers=headers) as r:
            if r.status != 200:
                raise RuntimeError(f"Extract HTTP {r.status}: {await r.text()}")
            return await r.json()


def _raw_fields(fields: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        f["FieldName"]: ", ".join(v["Value"] for v in f["Values"])
        for f in fields
        if not f.get("IsMissing") and f.get("Values")
    }


async def extract_id_fields(
    file_bytes: bytes,
    filename: str,
) -> Optional[Dict[str, Any]]:
    """
    Digitize a document and extract fields via UiPath Document Understanding.

    Returns a dict of {fieldName: value} as produced by the extractor,
    or None if DU is not configured, no extractor is available, or extraction fails.
    """
    if not _is_configured():
        logger.debug("UIPATH_DU_PROJECT_ID not set — skipping ID extraction")
        return None

    _load_cache()
    file_hash = _hash_bytes(file_bytes)
    cached = _cache.get(file_hash)
    if cached is not None:
        logger.info(f"DU cache hit for '{filename}' (hash={file_hash[:12]}) — reusing prior extraction")
        await asyncio.sleep(15)
        return cached

    try:
        token = await _get_token()
        document_id = await _digitize(file_bytes, filename, token)
        logger.info(f"DU digitized '{filename}' → documentId={document_id}")

        extractors = await _get_extractors(token)
        extractor = next((e for e in extractors if e.get("status") == "Available"), None)
        if not extractor:
            logger.warning("DU: no available extractor in project")
            return None

        logger.info(f"DU extracting '{filename}' with '{extractor['name']}'")
        result = await _extract(extractor["id"], document_id, token)

        fields = (
            result
            .get("extractionResult", {})
            .get("ResultsDocument", {})
            .get("Fields", [])
        )
        extracted = _raw_fields(fields) if fields else {}
        if extracted:
            logger.info(f"DU extracted fields {list(extracted.keys())} from '{filename}'")
            _cache[file_hash] = extracted
            _save_cache()
        return extracted or None

    except Exception as exc:
        logger.warning(f"DU extraction failed for '{filename}': {exc}")
        return None
