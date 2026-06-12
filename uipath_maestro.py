"""
UiPath Maestro / Orchestrator integration client.

Authenticates with UiPath Cloud via OAuth2 client-credentials,
starts a named process with citizen request data, polls for
completion, and returns the out_Reply output argument.

Expected process input arguments
---------------------------------
in_SessionId   : String  -- unique chat-session identifier (same across the whole chat)
in_Subject     : String  -- service category (e.g. "Passport Renewal")
in_CitizenData : String  -- JSON-serialised citizen profile dict
in_Documents   : String  -- JSON array of base64-encoded document bytes

Expected process output argument
----------------------------------
out_Reply      : String  -- text response relayed back to the citizen via the AI
"""

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class UiPathMaestroConfig:
    """Reads UiPath configuration from environment variables."""

    def __init__(self) -> None:
        self.base_url: str = os.getenv("UIPATH_URL", "https://cloud.uipath.com").rstrip("/")
        self.organization: str = os.getenv("UIPATH_ORGANIZATION", "")
        self.tenant: str = os.getenv("UIPATH_TENANT", "")
        self.client_id: str = os.getenv("UIPATH_CLIENT_ID", "")
        self.client_secret: str = os.getenv("UIPATH_CLIENT_SECRET", "")
        self.process_key: str = os.getenv("UIPATH_PROCESS_KEY", "")
        self.folder_name: str = os.getenv("UIPATH_FOLDER_NAME", "Default")
        # UIPATH_BASE_URL takes priority; fall back to UIPATH_URL (Stefano's alias)
        self.base_url: str = (
            os.getenv("UIPATH_BASE_URL") or
            os.getenv("UIPATH_URL") or
            "https://cloud.uipath.com"
        ).rstrip("/")
        self.bucket_name: str = os.getenv("UIPATH_BUCKET_NAME", "Document_Repository")

    @property
    def is_configured(self) -> bool:
        return all([
            self.organization, self.tenant,
            self.client_id, self.client_secret,
            self.process_key,
        ])


class UiPathMaestroClient:
    """Async client for UiPath Cloud Orchestrator / Maestro job execution."""

    _POLL_INTERVAL_S = 2
    _POLL_MAX_ATTEMPTS = 90  # up to 3 minutes
    # Disable brotli — not supported by all aiohttp builds
    _HEADERS_BASE = {"Accept-Encoding": "gzip, deflate"}

    def __init__(self, config: UiPathMaestroConfig) -> None:
        self.cfg = config
        self._token_url = f"{config.base_url}/identity_/connect/token"
        # Cached auth state — populated by warmup(), reused by submit()
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._folder_id: Optional[int] = None
        self._release_key: Optional[str] = None
        self._bucket_id: Optional[int] = None

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def warmup(self) -> None:
        """
        Pre-fetch token, folder ID, and release key so submit() is instant.
        Call this as a background task when a session opens.
        """
        if not self.cfg.is_configured:
            return
        try:
            token = await self._get_token()
            folder_id = await self._resolve_folder_id(token)
            await self._resolve_release_key(token, folder_id)
            await self._resolve_bucket_id(token, folder_id)
            logger.info("UiPath warmup complete: token + folder + release key + bucket cached")
        except Exception as exc:
            logger.warning(f"UiPath warmup failed (will retry on submit): {exc}")

    async def submit(
        self,
        session_id: str,
        subject: str,
        citizen_data: Dict[str, Any],
        documents: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        Start a UiPath job and wait for completion.
        documents: list of {"name": str, "data": bytes} dicts.
        Files are uploaded to the storage bucket; paths are passed via in_Documents.
        Returns the out_Reply string from the job output arguments.
        """
        if not self.cfg.is_configured:
            logger.warning("UiPath Maestro not configured — returning placeholder.")
            return (
                "The municipal processing system is not available at this time. "
                "Your request details have been noted. "
                "Please contact our office directly to complete your application."
            )

        try:
            token = await self._get_token()
            folder_id = await self._resolve_folder_id(token)
            release_key = await self._resolve_release_key(token, folder_id)

            # Upload documents to storage bucket and collect their paths
            uploaded_docs: List[Dict[str, str]] = []
            if documents:
                bucket_id = await self._resolve_bucket_id(token, folder_id)
                if bucket_id:
                    for doc in documents:
                        name = doc.get("name", f"document_{len(uploaded_docs)}")
                        data = doc.get("data", b"")
                        if not data:
                            continue
                        bucket_path = f"{session_id}/{name}"
                        try:
                            await self._upload_to_bucket(token, folder_id, bucket_id, bucket_path, data)
                            uploaded_docs.append({"name": name, "path": bucket_path})
                        except Exception as exc:
                            logger.warning(f"Failed to upload '{name}' to bucket: {exc}")
                else:
                    logger.warning("Storage bucket not found — documents will not be uploaded")

            enriched_citizen = {
                **citizen_data,
                "documentCount": len(uploaded_docs),
            }

            input_args: Dict[str, Any] = {
                "in_SessionId": session_id,
                "in_Subject": subject,
                "in_CitizenData": json.dumps(enriched_citizen, ensure_ascii=False),
                "in_Documents": json.dumps(uploaded_docs),
            }

            job_id = await self._start_job(token, folder_id, release_key, input_args)
            logger.info(f"UiPath job started: id={job_id}, session={session_id[:8]}, subject={subject}")
            return await self._poll_until_done(token, job_id)

        except Exception as exc:
            logger.error(f"UiPath submission failed: {exc}", exc_info=True)
            return (
                "There was an issue reaching the municipal processing system. "
                "Your request has been logged. "
                "A staff member will follow up with you by email or phone."
            )

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    async def _get_token(self) -> str:
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.cfg.client_id,
            "client_secret": self.cfg.client_secret,
            "scope": "OR.Execution OR.Folders OR.Jobs OR.Robots.Read OR.Buckets.Read OR.Buckets.Write OR.Administration",
        }
        async with aiohttp.ClientSession(headers=self._HEADERS_BASE) as s:
            async with s.post(self._token_url, data=payload) as r:
                if r.status != 200:
                    raise RuntimeError(f"UiPath auth error {r.status}: {await r.text()}")
                data = await r.json()
        self._token = data["access_token"]
        # Expire 60 s before the actual TTL to avoid using a token that's about to die
        self._token_expiry = time.monotonic() + data.get("expires_in", 3600) - 60
        return self._token

    async def _resolve_folder_id(self, token: str) -> Optional[int]:
        if self._folder_id is not None:
            return self._folder_id
        url = (
            f"{self.cfg.base_url}/{self.cfg.organization}/{self.cfg.tenant}"
            "/orchestrator_/odata/Folders"
        )
        params = {
            "$filter": f"DisplayName eq '{self.cfg.folder_name}'",
            "$select": "Id,DisplayName",
        }
        headers = {"Authorization": f"Bearer {token}"}
        async with aiohttp.ClientSession(headers=self._HEADERS_BASE) as s:
            async with s.get(url, headers=headers, params=params) as r:
                if r.status != 200:
                    logger.warning(f"Folder lookup HTTP {r.status}: {await r.text()}")
                    return None
                items = (await r.json()).get("value", [])
                if not items:
                    logger.warning(f"Folder '{self.cfg.folder_name}' not found")
                    return None
        self._folder_id = items[0]["Id"]
        logger.info(f"Folder resolved: {items[0]['DisplayName']} → id={self._folder_id}")
        return self._folder_id

    async def _resolve_release_key(
        self,
        token: str,
        folder_id: Optional[int],
    ) -> Optional[str]:
        if self._release_key is not None:
            return self._release_key
        url = (
            f"{self.cfg.base_url}/{self.cfg.organization}/{self.cfg.tenant}"
            "/orchestrator_/odata/Releases"
        )
        headers: Dict[str, str] = {"Authorization": f"Bearer {token}"}
        if folder_id is not None:
            headers["X-UIPATH-OrganizationUnitId"] = str(folder_id)
        for field in ("ProcessKey", "Name"):
            params = {
                "$filter": f"{field} eq '{self.cfg.process_key}'",
                "$select": "Key,Name,ProcessKey",
                "$top": "1",
            }
            async with aiohttp.ClientSession(headers=self._HEADERS_BASE) as s:
                async with s.get(url, headers=headers, params=params) as r:
                    if r.status != 200:
                        logger.warning(f"Release lookup by {field} HTTP {r.status}")
                        continue
                    items = (await r.json()).get("value", [])
                    if items:
                        self._release_key = items[0]["Key"]
                        logger.info(
                            f"Release resolved by {field}: name={items[0]['Name']}, "
                            f"key={self._release_key}"
                        )
                        return self._release_key
        logger.warning(
            f"No release found for '{self.cfg.process_key}' — will use ProcessKey in StartJobs"
        )
        return None

    async def _resolve_bucket_id(self, token: str, folder_id: Optional[int]) -> Optional[int]:
        if self._bucket_id is not None:
            return self._bucket_id
        url = (
            f"{self.cfg.base_url}/{self.cfg.organization}/{self.cfg.tenant}"
            "/orchestrator_/odata/Buckets"
        )
        params = {
            "$filter": f"Name eq '{self.cfg.bucket_name}'",
            "$select": "Id,Name",
            "$top": "1",
        }
        headers: Dict[str, str] = {"Authorization": f"Bearer {token}"}
        if folder_id is not None:
            headers["X-UIPATH-OrganizationUnitId"] = str(folder_id)
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, params=params) as r:
                if r.status != 200:
                    logger.warning(f"Bucket lookup HTTP {r.status}: {await r.text()}")
                    return None
                items = (await r.json()).get("value", [])
                if not items:
                    logger.warning(f"Bucket '{self.cfg.bucket_name}' not found in folder")
                    return None
        self._bucket_id = items[0]["Id"]
        logger.info(f"Bucket resolved: {items[0]['Name']} → id={self._bucket_id}")
        return self._bucket_id

    async def _upload_to_bucket(
        self,
        token: str,
        folder_id: Optional[int],
        bucket_id: int,
        file_path: str,
        data: bytes,
    ) -> None:
        import mimetypes

        file_name = file_path.rsplit("/", 1)[-1]
        content_type, _ = mimetypes.guess_type(file_name)
        if not content_type:
            content_type = "application/octet-stream"

        # Step 1: obtain a pre-signed write URI from Orchestrator.
        # contentType is a required parameter — its absence causes 404.
        # OR.Administration scope is required for upload (not just OR.Buckets).
        uri_url = (
            f"{self.cfg.base_url}/{self.cfg.organization}/{self.cfg.tenant}"
            f"/orchestrator_/odata/Buckets({bucket_id})"
            f"/UiPath.Server.Configuration.OData.GetWriteUri"
        )
        params = {"path": file_path, "contentType": content_type}
        headers: Dict[str, str] = {"Authorization": f"Bearer {token}"}
        if folder_id is not None:
            headers["X-UIPATH-OrganizationUnitId"] = str(folder_id)
        logger.debug(f"GetWriteUri → GET {uri_url} | params={params}")
        async with aiohttp.ClientSession() as s:
            async with s.get(uri_url, headers=headers, params=params) as r:
                body = await r.text()
                if r.status != 200:
                    raise RuntimeError(
                        f"GetWriteUri HTTP {r.status} | url={uri_url} | params={params} | body={body}"
                    )
                write_info = json.loads(body)

        write_uri: str = write_info.get("Uri") or write_info.get("uri", "")
        if not write_uri:
            raise RuntimeError(f"GetWriteUri returned no URI: {write_info}")

        # Step 2: upload using the verb and headers returned by GetWriteUri.
        # Do NOT include Authorization — the URI is pre-signed.
        verb: str = write_info.get("Verb", "PUT")
        upload_headers: Dict[str, str] = {"Content-Type": content_type}
        resp_headers = write_info.get("Headers", {})
        for k, v in zip(resp_headers.get("Keys", []), resp_headers.get("Values", [])):
            upload_headers[k] = v

        async with aiohttp.ClientSession() as s:
            method = getattr(s, verb.lower(), s.put)
            async with method(write_uri, data=data, headers=upload_headers) as r:
                if r.status not in (200, 201):
                    raise RuntimeError(f"Bucket upload HTTP {r.status}: {await r.text()}")
        logger.info(f"Uploaded to bucket: {file_path} ({len(data)} bytes)")

    async def _start_job(
        self,
        token: str,
        folder_id: Optional[int],
        release_key: Optional[str],
        input_args: Dict[str, Any],
    ) -> int:
        url = (
            f"{self.cfg.base_url}/{self.cfg.organization}/{self.cfg.tenant}"
            "/orchestrator_/odata/Jobs/UiPath.Server.Configuration.OData.StartJobs"
        )
        headers: Dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if folder_id is not None:
            headers["X-UIPATH-OrganizationUnitId"] = str(folder_id)

        start_info: Dict[str, Any] = {
            "Strategy": "ModernJobsCount",
            "JobsCount": 1,
            "InputArguments": json.dumps(input_args),
        }
        if release_key:
            start_info["ReleaseKey"] = release_key
        else:
            start_info["ProcessKey"] = self.cfg.process_key

        body = {"startInfo": start_info}
        async with aiohttp.ClientSession(headers=self._HEADERS_BASE) as s:
            async with s.post(url, json=body, headers=headers) as r:
                if r.status not in (200, 201):
                    raise RuntimeError(f"StartJobs HTTP {r.status}: {await r.text()}")
                jobs = (await r.json()).get("value", [])
                if not jobs:
                    raise RuntimeError("StartJobs returned no job entries.")
                return jobs[0]["Id"]

    async def _poll_until_done(self, token: str, job_id: int) -> str:
        url = (
            f"{self.cfg.base_url}/{self.cfg.organization}/{self.cfg.tenant}"
            f"/orchestrator_/odata/Jobs({job_id})"
        )
        headers = {"Authorization": f"Bearer {token}"}

        for attempt in range(self._POLL_MAX_ATTEMPTS):
            await asyncio.sleep(self._POLL_INTERVAL_S)
            try:
                async with aiohttp.ClientSession(headers=self._HEADERS_BASE) as s:
                    async with s.get(url, headers=headers) as r:
                        if r.status != 200:
                            logger.debug(f"Poll {attempt+1}: HTTP {r.status}")
                            continue
                        data = await r.json()

                state: str = data.get("State", "Pending")
                logger.debug(f"Job {job_id} state={state} attempt={attempt+1}")

                if state == "Successful":
                    raw = data.get("OutputArguments") or "{}"
                    try:
                        out = json.loads(raw) if isinstance(raw, str) else raw
                    except json.JSONDecodeError:
                        return "Your request has been registered successfully with the municipal system."
                    return (
                        out.get("out_Reply")
                        or "Your request has been registered. You will be contacted shortly."
                    )

                if state in ("Faulted", "Failed", "Stopped", "Terminated"):
                    logger.error(f"UiPath job {job_id} ended with state: {state}")
                    return (
                        "The processing system encountered an issue with your request. "
                        "Please visit our office or call the citizen helpline for further assistance."
                    )

            except Exception as exc:
                logger.warning(f"Poll attempt {attempt+1} error: {exc}")

        logger.warning(f"Job {job_id} did not complete within polling window.")
        return (
            "Your request has been submitted and is currently being processed. "
            "This may take some time. You will be notified via email once completed."
        )
