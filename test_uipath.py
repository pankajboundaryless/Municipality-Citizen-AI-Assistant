"""
Standalone diagnostic test for the UiPath Maestro integration.
Run from the project root:  python test_uipath.py
Reads credentials from .env — no server startup required.

Prints every URL and request body so you can spot misconfigured env vars
before the real call is made.
"""

import asyncio
import json
import logging
import mimetypes
import pathlib
import uuid

import aiohttp
from dotenv import load_dotenv

load_dotenv()

from uipath_maestro import UiPathMaestroConfig

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s  %(name)s  %(message)s")

# ── Mock data ─────────────────────────────────────────────────────────────────

SESSION_ID = str(uuid.uuid4())
SUBJECT    = "Passport Renewal"

# Mock document: read log.txt from the project root (or an empty fallback)
_LOG_FILE = pathlib.Path(__file__).parent / "log.txt"
MOCK_DOCUMENT_NAME = "log.txt"
MOCK_DOCUMENT_DATA = _LOG_FILE.read_bytes() if _LOG_FILE.exists() else b"mock document content"

MOCK_CITIZEN_DATA = {
    "name": "Maria Rossi",
    "idNumber": "IT-7654321",
    "email": "maria.rossi@example.com",
    "phone": "+39 06 1234 5678",
    "selectedService": "Passport",
    "preferredLanguage": "it-IT",
    "requestSummary": (
        "Citizen requests a passport renewal. Current passport number IT-7654321 "
        "expires on 15/03/2025. Planned travel date 20/07/2026. "
        "Proof of address and copy of expiring passport uploaded. "
        "One photo captured via webcam."
    ),
    "capturedPhotoCount": 1,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")

def mask(value: str) -> str:
    if len(value) <= 8:
        return "****"
    return value[:4] + "*" * (len(value) - 8) + value[-4:]

# ── Steps ─────────────────────────────────────────────────────────────────────

async def step_config(cfg: UiPathMaestroConfig) -> bool:
    section("1 · Config (from .env)")
    print(f"  UIPATH_URL           = {cfg.base_url}")
    print(f"  UIPATH_ORGANIZATION  = {cfg.organization  or '(not set)'}")
    print(f"  UIPATH_TENANT        = {cfg.tenant        or '(not set)'}")
    print(f"  UIPATH_CLIENT_ID     = {cfg.client_id     or '(not set)'}")
    print(f"  UIPATH_CLIENT_SECRET = {mask(cfg.client_secret) if cfg.client_secret else '(not set)'}")
    print(f"  UIPATH_PROCESS_KEY   = {cfg.process_key   or '(not set)'}")
    print(f"  UIPATH_FOLDER_NAME   = {cfg.folder_name}")
    if not cfg.is_configured:
        print("\n  ERROR: one or more required env vars are missing. Aborting.")
        return False
    return True


async def step_token(cfg: UiPathMaestroConfig) -> str | None:
    section("2 · OAuth2 token")
    url = f"{cfg.base_url}/identity_/connect/token"
    payload = {
        "grant_type":    "client_credentials",
        "client_id":     cfg.client_id,
        "client_secret": cfg.client_secret,
        "scope":         "OR.Execution OR.Folders OR.Jobs OR.Robots.Read OR.Buckets.Read OR.Buckets.Write OR.Administration",
    }
    print(f"  POST {url}")
    print(f"  scope: {payload['scope']}")
    async with aiohttp.ClientSession() as s:
        async with s.post(url, data=payload) as r:
            body = await r.text()
            print(f"  HTTP {r.status}")
            if r.status != 200:
                print(f"  RESPONSE: {body}")
                return None
            token = json.loads(body)["access_token"]
            print(f"  Token obtained: {mask(token)}")
            return token


async def step_folder(cfg: UiPathMaestroConfig, token: str) -> int | None:
    section("3 · Folder lookup")
    url = (
        f"{cfg.base_url}/{cfg.organization}/{cfg.tenant}"
        "/orchestrator_/odata/Folders"
    )
    params = {
        "$filter": f"DisplayName eq '{cfg.folder_name}'",
        "$select": "Id,DisplayName",
    }
    headers = {"Authorization": f"Bearer {token}"}
    print(f"  GET {url}")
    print(f"  filter: {params['$filter']}")
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers, params=params) as r:
            body = await r.text()
            print(f"  HTTP {r.status}")
            if r.status != 200:
                print(f"  RESPONSE: {body}")
                if r.status == 403:
                    print("\n  ── 403 fix checklist ──────────────────────────────────")
                    print("  The token was issued but the app has no Orchestrator role.")
                    print("  In UiPath Automation Cloud:")
                    print("  1. Admin → External Applications → open your app")
                    print("     Under 'Resources' enable:")
                    print("       OR.Execution  OR.Folders  OR.Jobs  OR.Robots.Read")
                    print("  2. Orchestrator → Tenant → Manage Access → Assign Roles")
                    print("     Add the app's client_id as a Robot Account and grant")
                    print("     the 'Allow to be Automation User' role (or 'Automation User').")
                    print("  3. In the target Folder → Manage Access, add the Robot")
                    print("     Account with at least 'Automation User' role.")
                else:
                    print("\n  Hint: check UIPATH_ORGANIZATION and UIPATH_TENANT are the")
                    print("  short logical names (e.g. 'mycompany', 'DefaultTenant'),")
                    print("  NOT full URLs.")
                return None
            items = json.loads(body).get("value", [])
            if not items:
                print(f"  Folder '{cfg.folder_name}' not found.")
                print("  Check UIPATH_FOLDER_NAME in .env (default: 'Default').")
                return None
            folder_id = items[0]["Id"]
            print(f"  Folder ID: {folder_id}  ({items[0]['DisplayName']})")
            return folder_id


async def step_release(
    cfg: UiPathMaestroConfig,
    token: str,
    folder_id: int,
) -> str | None:
    section("4 · Release lookup (process name → GUID)")
    url = (
        f"{cfg.base_url}/{cfg.organization}/{cfg.tenant}"
        "/orchestrator_/odata/Releases"
    )
    headers = {
        "Authorization":               f"Bearer {token}",
        "X-UIPATH-OrganizationUnitId": str(folder_id),
    }

    release_key = None
    for field in ("ProcessKey", "Name"):
        params = {
            "$filter": f"{field} eq '{cfg.process_key}'",
            "$select": "Key,Name,ProcessKey",
            "$top":    "1",
        }
        print(f"  GET {url}")
        print(f"  filter: {params['$filter']}")
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, params=params) as r:
                body = await r.text()
                print(f"  HTTP {r.status}")
                if r.status != 200:
                    print(f"  RESPONSE: {body}")
                    continue
                items = json.loads(body).get("value", [])
                if items:
                    release_key = items[0]["Key"]
                    print(f"  Found by {field}: name={items[0]['Name']}")
                    print(f"  ReleaseKey (GUID): {release_key}")
                    return release_key
                print(f"  No match for {field} = '{cfg.process_key}'")

    print(
        f"\n  WARNING: process '{cfg.process_key}' not found in folder.\n"
        "  Check UIPATH_PROCESS_KEY matches the process Name or ProcessKey shown\n"
        "  in Orchestrator → Automations → Processes."
    )
    return None


async def step_bucket(cfg: UiPathMaestroConfig, token: str, folder_id: int) -> int | None:
    section("5 · Bucket lookup")
    url = (
        f"{cfg.base_url}/{cfg.organization}/{cfg.tenant}"
        "/orchestrator_/odata/Buckets"
    )
    params = {
        "$filter": f"Name eq '{cfg.bucket_name}'",
        "$select": "Id,Name",
        "$top":    "1",
    }
    headers = {
        "Authorization":               f"Bearer {token}",
        "X-UIPATH-OrganizationUnitId": str(folder_id),
    }
    print(f"  GET {url}")
    print(f"  filter: {params['$filter']}")
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers, params=params) as r:
            body = await r.text()
            print(f"  HTTP {r.status}")
            if r.status != 200:
                print(f"  RESPONSE: {body}")
                return None
            items = json.loads(body).get("value", [])
            if not items:
                print(f"  Bucket '{cfg.bucket_name}' not found in folder.")
                return None
            bucket_id = items[0]["Id"]
            print(f"  Bucket ID: {bucket_id}  ({items[0]['Name']})")
            return bucket_id


async def step_upload(
    cfg: UiPathMaestroConfig,
    token: str,
    folder_id: int,
    bucket_id: int,
) -> list[dict] | None:
    section("6 · Upload mock document to bucket")
    file_path = f"{SESSION_ID}/{MOCK_DOCUMENT_NAME}"
    print(f"  File      : {MOCK_DOCUMENT_NAME}  ({len(MOCK_DOCUMENT_DATA)} bytes)")
    print(f"  Bucket path: {file_path}")

    content_type, _ = mimetypes.guess_type(MOCK_DOCUMENT_NAME)
    if not content_type:
        content_type = "application/octet-stream"

    # Step 6a — get pre-signed write URI (contentType is a required parameter)
    uri_url = (
        f"{cfg.base_url}/{cfg.organization}/{cfg.tenant}"
        f"/orchestrator_/odata/Buckets({bucket_id})"
        f"/UiPath.Server.Configuration.OData.GetWriteUri"
    )
    params = {"path": file_path, "contentType": content_type}
    headers = {
        "Authorization":               f"Bearer {token}",
        "X-UIPATH-OrganizationUnitId": str(folder_id),
    }
    print(f"\n  GET {uri_url}")
    print(f"  params: {params}")
    async with aiohttp.ClientSession() as s:
        async with s.get(uri_url, headers=headers, params=params) as r:
            body = await r.text()
            print(f"  HTTP {r.status}")
            if r.status != 200:
                print(f"  RESPONSE: {body}")
                return None
            write_info = json.loads(body)

    write_uri = write_info.get("Uri") or write_info.get("uri", "")
    verb: str = write_info.get("Verb", "PUT")
    print(f"  Write URI obtained: {write_uri[:80]}...")
    print(f"  Verb: {verb}")

    # Step 6b — upload using verb and headers from response; no Authorization header
    upload_headers: dict = {"Content-Type": content_type}
    resp_headers = write_info.get("Headers", {})
    for k, v in zip(resp_headers.get("Keys", []), resp_headers.get("Values", [])):
        upload_headers[k] = v
    print(f"  Upload headers: {upload_headers}")
    print(f"\n  {verb} {write_uri[:80]}...")
    async with aiohttp.ClientSession() as s:
        method = getattr(s, verb.lower(), s.put)
        async with method(write_uri, data=MOCK_DOCUMENT_DATA, headers=upload_headers) as r:
            body = await r.text()
            print(f"  HTTP {r.status}")
            if r.status not in (200, 201):
                print(f"  RESPONSE: {body}")
                return None

    print(f"  Upload successful.")
    return [{"name": MOCK_DOCUMENT_NAME, "path": file_path}]


async def step_start_job(
    cfg: UiPathMaestroConfig,
    token: str,
    folder_id: int,
    release_key: str | None,
    uploaded_docs: list[dict] | None = None,
) -> int | None:
    section("7 · Start job")
    url = (
        f"{cfg.base_url}/{cfg.organization}/{cfg.tenant}"
        "/orchestrator_/odata/Jobs/UiPath.Server.Configuration.OData.StartJobs"
    )
    headers = {
        "Authorization":               f"Bearer {token}",
        "Content-Type":                "application/json",
        "X-UIPATH-OrganizationUnitId": str(folder_id),
    }

    citizen = {**MOCK_CITIZEN_DATA, "documentCount": len(uploaded_docs or [])}
    input_args = {
        "in_SessionId":   SESSION_ID,
        "in_Subject":     SUBJECT,
        "in_CitizenData": json.dumps(citizen, ensure_ascii=False),
        "in_Documents":   json.dumps(uploaded_docs or []),
    }

    start_info: dict = {
        "Strategy":       "ModernJobsCount",
        "JobsCount":      1,
        "InputArguments": json.dumps(input_args),
    }
    if release_key:
        start_info["ReleaseKey"] = release_key
        print(f"  Using ReleaseKey (GUID): {release_key}")
    else:
        start_info["ProcessKey"] = cfg.process_key
        print(f"  ReleaseKey not found — falling back to ProcessKey: {cfg.process_key}")

    body = {"startInfo": start_info}

    print(f"  POST {url}")
    print(f"  X-UIPATH-OrganizationUnitId: {folder_id}")
    display = json.loads(json.dumps(body))
    display["startInfo"]["InputArguments"] = "<see below>"
    print("\n  Request body:")
    print("  " + json.dumps(display, indent=4).replace("\n", "\n  "))
    print("\n  InputArguments (expanded):")
    print("  " + json.dumps(input_args, indent=4, ensure_ascii=False).replace("\n", "\n  "))

    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=body, headers=headers) as r:
            resp_body = await r.text()
            print(f"\n  HTTP {r.status}")
            if r.status not in (200, 201):
                print(f"  RESPONSE: {resp_body}")
                if r.status == 403:
                    print("\n  ── 403 fix checklist ──────────────────────────────────")
                    print("  Token is valid but the app cannot start jobs in this folder.")
                    print("  In Orchestrator → Folder → Manage Access:")
                    print("    Add the Robot Account (same client_id) with role")
                    print("    'Automation User' or a custom role that includes")
                    print("    'Jobs: Create' and 'Jobs: View' permissions.")
                return None
            jobs = json.loads(resp_body).get("value", [])
            if not jobs:
                print("  StartJobs returned no job entries.")
                return None
            job_id = jobs[0]["Id"]
            print(f"  Job started — ID: {job_id}")
            return job_id


async def step_poll(cfg: UiPathMaestroConfig, token: str, job_id: int) -> str:
    section("8 · Polling job state")
    url = (
        f"{cfg.base_url}/{cfg.organization}/{cfg.tenant}"
        f"/orchestrator_/odata/Jobs({job_id})"
    )
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(1, 91):
        await asyncio.sleep(2)
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers) as r:
                if r.status != 200:
                    print(f"  Poll {attempt}: HTTP {r.status}")
                    continue
                data = await r.json()
        state = data.get("State", "Pending")
        print(f"  Poll {attempt:>2}: state = {state}")
        if state == "Successful":
            raw = data.get("OutputArguments") or "{}"
            out = json.loads(raw) if isinstance(raw, str) else raw
            return out.get("out_Reply") or "Job completed — no out_Reply value returned."
        if state in ("Faulted", "Failed", "Stopped", "Terminated"):
            return f"Job ended with state: {state}"
    return "Polling timed out after 3 minutes."


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("\n=== UiPath Maestro Diagnostic Test ===")
    print(f"  Session ID : {SESSION_ID}")
    print(f"  Subject    : {SUBJECT}")

    cfg = UiPathMaestroConfig()

    if not await step_config(cfg):
        return

    token = await step_token(cfg)
    if not token:
        return

    folder_id = await step_folder(cfg, token)
    if not folder_id:
        return

    release_key = await step_release(cfg, token, folder_id)
    # release_key may be None — will fall back to ProcessKey in StartJobs

    bucket_id = await step_bucket(cfg, token, folder_id)
    # bucket_id may be None — job will still start without documents

    uploaded_docs = None
    if bucket_id:
        uploaded_docs = await step_upload(cfg, token, folder_id, bucket_id)

    job_id = await step_start_job(cfg, token, folder_id, release_key, uploaded_docs)
    if not job_id:
        return

    reply = await step_poll(cfg, token, job_id)

    section("Result")
    print(f"  out_Reply: {reply}")


if __name__ == "__main__":
    asyncio.run(main())
