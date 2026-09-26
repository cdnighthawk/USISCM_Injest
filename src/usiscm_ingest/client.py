"""HTTP client for USIS Construction Management.

Drawings follow the USISPdfApp path: catalog row on the website, PDF bytes
straight to native B2, then ack. Documents use the ingest API (or the
session ingest route). Automatic drawing names never wait for a person;
ambiguous names still upload and are logged on ``/api/v1/ingest/errors``
so they show up in the CM ingest tracker.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urljoin

import requests

from usiscm_ingest.b2 import (
    B2_UPLOAD_URL_UNAVAILABLE,
    S3_FALLBACK_FORBIDDEN,
    B2Error,
    file_sha256,
    parse_upload_hint,
    post_file,
    sha256_hex,
)
from usiscm_ingest.classify import FileCategory
from usiscm_ingest.config import Settings
from usiscm_ingest.drawing_namer import (
    DrawingName,
    apply_ai_identity,
    name_drawing,
    title_block_jpeg_base64,
)
from usiscm_ingest.microsoft import (
    UNATTENDED_HINT,
    EntraApp,
    MicrosoftAuthError,
    discover_entra_app,
    load_tokens,
    resolve_access_token,
)
from usiscm_ingest.package import PackageManifest

logger = logging.getLogger(__name__)

DRAWING_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
DOCUMENT_EXTENSIONS = {".pdf", ".dwg", ".doc", ".docx", ".xls", ".xlsx", ".png", ".jpg", ".jpeg"}
ISSUE_SOURCE = "usiscm_ingest"
_MINT_BACKOFF = (2, 8, 30)


class UsiscmError(RuntimeError):
    pass


@dataclass
class UploadResult:
    project_id: str
    imported: int = 0
    skipped: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    batch_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "imported": self.imported,
            "skipped": self.skipped,
            "errors": self.errors,
            "details": self.details,
            "issues": self.issues,
            "batch_id": self.batch_id,
        }


class UsiscmClient:
    def __init__(
        self,
        settings: Settings,
        timeout: int = 120,
        *,
        sleeper: Callable[[float], None] | None = None,
        b2_post: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.settings = settings
        self.base_url = settings.base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.session = requests.Session()
        self.ingest_session = requests.Session()
        self.token: str | None = None
        self.auth_mode: str | None = None
        self._entra: EntraApp | None = None
        self._sleeper = sleeper or time.sleep
        self._b2_post = b2_post or post_file
        self._ai_available = True

    @property
    def uses_ingest_key(self) -> bool:
        return self.auth_mode == "ingest_key"

    @property
    def uses_microsoft(self) -> bool:
        return self.auth_mode == "microsoft"

    def entra_app(self) -> EntraApp:
        if self._entra is not None:
            return self._entra
        if self.settings.ms_tenant_id and self.settings.ms_client_id:
            self._entra = EntraApp(self.settings.ms_tenant_id, self.settings.ms_client_id)
            return self._entra
        cached = load_tokens(self.settings.token_path)
        if cached and cached.tenant_id and cached.client_id:
            self._entra = EntraApp(cached.tenant_id, cached.client_id, cached.scopes)
            return self._entra
        try:
            self._entra = discover_entra_app(self.settings.base_url, timeout=self.timeout)
        except MicrosoftAuthError as exc:
            raise UsiscmError(str(exc)) from exc
        return self._entra

    def login(self, *, interactive: bool = False) -> str:
        microsoft_error: MicrosoftAuthError | None = None
        can_try_ms = bool(
            self.settings.ms_access_token
            or self.settings.token_path.exists()
            or interactive
            or (self.settings.ms_tenant_id and self.settings.ms_client_id)
        )
        if can_try_ms or not self.settings.ingest_api_key:
            try:
                token = resolve_access_token(
                    self.entra_app(),
                    token_path=self.settings.token_path,
                    access_token=self.settings.ms_access_token or None,
                    interactive=interactive,
                )
                self.token = token
                self.auth_mode = "microsoft"
                self.session.headers["Authorization"] = f"Bearer {token}"
                if self.settings.ingest_api_key:
                    self.ingest_session.headers["Authorization"] = f"Bearer {self.settings.ingest_api_key}"
                return token
            except (MicrosoftAuthError, UsiscmError) as exc:
                microsoft_error = MicrosoftAuthError(str(exc))
                if not self.settings.ingest_api_key:
                    raise UsiscmError(str(exc)) from exc
                logger.warning("Microsoft session unavailable (%s); using ingest API key", exc)

        if self.settings.ingest_api_key:
            self.token = self.settings.ingest_api_key
            self.auth_mode = "ingest_key"
            self.session.headers["Authorization"] = f"Bearer {self.token}"
            self.ingest_session.headers["Authorization"] = f"Bearer {self.token}"
            return self.token
        raise UsiscmError(str(microsoft_error) if microsoft_error else UNATTENDED_HINT)

    def auth_status(self) -> dict[str, Any]:
        self._ensure_auth()
        if self.uses_ingest_key:
            return {"authenticated": True, "mode": "ingest_key"}
        response = self.session.get(self._url("api/v1/auth/status"), timeout=self.timeout)
        payload = _json(response)
        if not response.ok:
            raise UsiscmError(payload.get("error") or f"Auth status failed ({response.status_code})")
        return payload

    def list_projects(self, *, limit: int = 500, query: str = "") -> list[dict[str, Any]]:
        self._ensure_auth()
        if self.uses_ingest_key or (self.settings.ingest_api_key and not query):
            session = self.ingest_session if self.settings.ingest_api_key and self.uses_microsoft else self.session
            if self.settings.ingest_api_key and self.uses_microsoft:
                params = {"q": query} if query else {}
                response = session.get(self._url("api/projects"), params=params, timeout=self.timeout)
                if response.ok:
                    payload = _json(response)
                    return payload.get("projects") or payload.get("items") or []
            if self.uses_ingest_key:
                params = {"q": query} if query else {}
                response = self.session.get(self._url("api/projects"), params=params, timeout=self.timeout)
                payload = _json(response)
                if not response.ok:
                    raise UsiscmError(payload.get("error") or f"Project list failed ({response.status_code})")
                return payload.get("projects") or payload.get("items") or []

        collected: list[dict[str, Any]] = []
        offset = 0
        while True:
            params: dict[str, Any] = {"limit": limit, "offset": offset}
            if query:
                params["q"] = query
            response = self.session.get(self._url("api/v1/projects"), params=params, timeout=self.timeout)
            payload = _json(response)
            if not response.ok:
                raise UsiscmError(payload.get("error") or f"Project list failed ({response.status_code})")
            batch = payload.get("items") or payload.get("data") or payload.get("projects") or []
            collected.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
            if offset > 5000:
                break
        return collected

    def resolve_project(
        self,
        *,
        project_id: str | None = None,
        project_name: str | None = None,
    ) -> dict[str, Any]:
        if project_id:
            self._ensure_auth()
            matches = self.list_projects(query=str(project_id))
            for project in matches:
                if str(project.get("id") or project.get("project_id")) == str(project_id):
                    return project
            if self.uses_microsoft:
                response = self.session.get(self._url(f"api/v1/projects/{project_id}"), timeout=self.timeout)
                payload = _json(response)
                if response.ok:
                    return payload.get("item") or payload.get("data") or payload
            raise UsiscmError(f"Project {project_id} not found")

        if not project_name:
            raise UsiscmError("Provide --project-id or a package label to match")

        projects = self.list_projects()
        match = _best_project_match(project_name, projects)
        if match is None:
            raise UsiscmError(f"No USISCM project matched '{project_name}'. Pass --project-id.")
        return match

    def import_package(
        self,
        manifest: PackageManifest,
        *,
        project_id: str | None = None,
        dry_run: bool = False,
        batch_id: str | None = None,
    ) -> UploadResult:
        project = self.resolve_project(project_id=project_id, project_name=manifest.label)
        resolved_id = str(project.get("id") or project.get("project_id") or project_id or "")
        result = UploadResult(project_id=resolved_id, batch_id=batch_id or uuid.uuid4().hex[:16])
        if dry_run:
            named = []
            for item in manifest.files_for(FileCategory.DRAWING):
                named.append(name_drawing(filename=item.path.name, folder_path=item.relative_path).to_dict())
            result.details.append(
                {
                    "dry_run": True,
                    "project": project.get("name") or project.get("project_name"),
                    "counts": manifest.counts,
                    "names": named,
                }
            )
            result.imported = len(manifest.files)
            return result

        stored: list[tuple[str, dict[str, Any]]] = []
        drawings = [
            item
            for item in manifest.files_for(FileCategory.DRAWING)
            if item.path.suffix.lower() in DRAWING_EXTENSIONS
        ]
        for item in drawings:
            detail = self._upload_drawing(project, item, manifest.label, batch_id=result.batch_id)
            result.details.append(detail)
            stored.append((str(item.path), detail))

        for category in (
            FileCategory.SPEC,
            FileCategory.BID_INSTRUCTIONS,
            FileCategory.ADDENDA,
            FileCategory.REPORT,
            FileCategory.SCHEDULE,
            FileCategory.OTHER,
        ):
            docs = [
                item
                for item in manifest.files_for(category)
                if item.path.suffix.lower() in DOCUMENT_EXTENSIONS
            ]
            skipped = [
                item
                for item in manifest.files_for(category)
                if item.path.suffix.lower() not in DOCUMENT_EXTENSIONS
            ]
            result.skipped += len(skipped)
            for item in docs:
                detail = self._upload_document(
                    project, item, category, manifest.label, batch_id=result.batch_id
                )
                result.details.append(detail)
                stored.append((str(item.path), detail))

        for detail in result.details:
            result.imported += int(detail.get("imported") or 0)
            if detail.get("error"):
                result.errors.append({"filename": detail.get("filename"), "error": detail["error"]})
            result.errors.extend(detail.get("errors") or [])
            if detail.get("issue"):
                result.issues.append(detail["issue"])
        # One specialty-takeoff job per clean batch. Queue errors stay in the log.
        self._enqueue_specialty_takeoff(result, project, manifest, stored)
        return result

    def _enqueue_specialty_takeoff(
        self,
        result: UploadResult,
        project: dict[str, Any],
        manifest: PackageManifest,
        stored: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """Enqueue one ``usis.specialty_takeoff.v1`` job after a clean upload.

        Grain is one ``import_package`` call (one project, the files in that
        run). ``import`` and ``watch`` both come through here. ``folder_path``
        stays null; this app does not invent an estimate-folder location.
        """
        if result.errors or result.imported <= 0:
            return
        paths: list[str] = []
        file_ids: list[str] = []
        for path, detail in stored:
            if detail.get("error") or detail.get("errors") or not int(detail.get("imported") or 0):
                continue
            paths.append(path)
            file_id = detail.get("drawing_id") or detail.get("document_id") or detail.get("file_id")
            if file_id:
                file_ids.append(str(file_id))
        if not paths:
            return
        try:
            from usiscm_ingest.specialty_takeoff import enqueue_specialty_takeoff

            enqueue_specialty_takeoff(
                project_key=_specialty_project_key(project, manifest),
                project_id=result.project_id or None,
                estimate_id=_estimate_id(project),
                folder_path=None,
                source_paths=paths,
                specialties=["all"],
                trigger="ingest_ok",
                file_ids=file_ids,
                extra={"batch_id": result.batch_id} if result.batch_id else None,
            )
        except Exception as exc:
            logger.warning("specialty takeoff enqueue failed: %s", exc)

    def report_issue(
        self,
        *,
        message: str,
        relative_path: str = "",
        filename: str = "",
        kind: str = "",
        batch_id: str | None = None,
        project_id: str | None = None,
        project_number: str = "",
        http_status: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Log a review item onto the website ingest tracker. Microsoft session required."""
        if not self.uses_microsoft:
            logger.warning("Cannot post ingest issue without a Microsoft session: %s", message)
            return None
        body: dict[str, Any] = {
            "source": ISSUE_SOURCE,
            "message": message[:4000],
            "relative_path": relative_path,
            "filename": filename,
            "kind": kind[:20],
            "batch_id": batch_id,
            "project_id": project_id,
            "project_number": project_number,
            "detail": detail or {},
        }
        if http_status is not None:
            body["http_status"] = http_status
        try:
            response = self.session.post(self._url("api/v1/ingest/errors"), json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("ingest issue post failed: %s", exc)
            return None
        payload = _json(response)
        if not response.ok:
            logger.warning("ingest issue post failed (%s): %s", response.status_code, payload.get("error"))
            return None
        return payload.get("item") or payload

    def _upload_drawing(self, project: dict[str, Any], item, label: str, *, batch_id: str) -> dict[str, Any]:
        self._ensure_auth()
        named = name_drawing(
            filename=item.path.name,
            folder_path=item.relative_path,
            sheet_number=item.sheet_number,
        )
        named = self._maybe_ai_name(item.path, named)
        project_id = str(project.get("id") or project.get("project_id") or "")
        project_number = _project_number(project)
        issue = None
        if named.needs_review:
            issue = self.report_issue(
                message=named.review_message(),
                relative_path=item.relative_path,
                filename=item.path.name,
                kind="naming",
                batch_id=batch_id,
                project_id=project_id,
                project_number=project_number,
                detail=named.to_dict(),
            )
        try:
            if self.uses_microsoft:
                uploaded = self._upload_drawing_native_b2(project, item, named, label)
            else:
                uploaded = self._upload_drawing_ingest_key(project_id, item, named, label)
        except (UsiscmError, B2Error, OSError) as exc:
            code = getattr(exc, "code", None)
            message = str(exc)
            self.report_issue(
                message=message,
                relative_path=item.relative_path,
                filename=item.path.name,
                kind="drawing",
                batch_id=batch_id,
                project_id=project_id,
                project_number=project_number,
                detail={"code": code, "name": named.to_dict()},
            )
            return {
                "endpoint": "b2-native" if self.uses_microsoft else "ingest/drawings",
                "filename": item.path.name,
                "imported": 0,
                "error": message,
                "name": named.to_dict(),
                "issue": issue,
            }
        uploaded["name"] = named.to_dict()
        if issue:
            uploaded["issue"] = issue
        return uploaded

    def _maybe_ai_name(self, path, named: DrawingName) -> DrawingName:
        if not self.settings.sheet_ai or not self.uses_microsoft or not self._ai_available:
            return named
        if path.suffix.lower() != ".pdf":
            return named
        jpeg = title_block_jpeg_base64(path)
        body = {
            "items": [
                {
                    "rowId": str(uuid.uuid4()),
                    "sourceFileName": path.name,
                    "proposedSheetNumber": named.sheet_number,
                    "proposedSheetTitle": named.sheet_title,
                    "titleBlockJpegBase64": jpeg,
                }
            ]
        }
        try:
            response = self.session.post(self._url("api/v1/ai/sheet-identity"), json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.info("sheet-identity skipped: %s", exc)
            self._ai_available = False
            return named
        if response.status_code == 503:
            self._ai_available = False
            return named
        if not response.ok:
            logger.info("sheet-identity skipped (%s)", response.status_code)
            return named
        payload = _json(response)
        items = payload.get("items") or []
        identity = items[0] if items else None
        return apply_ai_identity(named, identity if isinstance(identity, dict) else None)

    def _upload_drawing_native_b2(self, project: dict[str, Any], item, named: DrawingName, label: str) -> dict[str, Any]:
        job_id = _job_id(project)
        if not job_id:
            raise UsiscmError("project has no job id for native B2 drawing create")
        payload = item.path.read_bytes()
        digest = sha256_hex(payload)
        create_body = {
            "item": {
                "sheetNumber": named.sheet_number,
                "sheetTitle": named.sheet_title,
                "revision": named.revision,
                "drawingSet": named.drawing_set or label,
                "sourceFileName": item.path.name,
                "discipline": named.discipline,
                "contentHash": digest,
            }
        }
        response = self.session.post(
            self._url(f"api/v1/jobs/{job_id}/drawings"),
            json=create_body,
            timeout=self.timeout,
        )
        created = _json(response)
        if response.status_code not in {200, 201}:
            raise UsiscmError(created.get("error") or f"drawing create failed ({response.status_code})")
        drawing = created.get("item") or created.get("drawing") or {}
        drawing_id = str(drawing.get("id") or drawing.get("drawing_id") or "")
        if not drawing_id:
            raise UsiscmError("website accepted the drawing row but returned no id")
        hint = created.get("upload")
        if not hint:
            hint = self._mint_upload(drawing_id)
        stored = self._post_b2_with_retry(drawing_id, hint, payload)
        ack = self.session.post(
            self._url(f"api/v1/drawings/{drawing_id}/ack-file"),
            json={
                "byte_size": len(payload),
                "content_hash": digest,
                "item": {
                    "b2FileId": stored.get("fileId"),
                    "b2FileName": stored.get("fileName"),
                    "contentSha1": stored.get("contentSha1"),
                    "contentLength": len(payload),
                    "sha256": digest,
                    "contentType": "application/pdf",
                },
            },
            timeout=self.timeout,
        )
        ack_payload = _json(ack)
        if not ack.ok:
            raise UsiscmError(ack_payload.get("error") or f"drawing ack failed ({ack.status_code})")
        return {
            "endpoint": "b2-native",
            "filename": item.path.name,
            "imported": 1,
            "drawing_id": drawing_id,
            "errors": [],
        }

    def _mint_upload(self, drawing_id: str) -> dict[str, Any]:
        last: dict[str, Any] | None = None
        for attempt, wait in enumerate(_MINT_BACKOFF, start=1):
            response = self.session.post(
                self._url(f"api/v1/drawings/{drawing_id}/upload-session"),
                timeout=self.timeout,
            )
            payload = _json(response)
            if response.ok and (payload.get("upload") or payload.get("url") or payload.get("mode")):
                return payload.get("upload") or payload
            last = payload
            if response.status_code == 503 or str(payload.get("error") or "") == B2_UPLOAD_URL_UNAVAILABLE:
                retry_after = response.headers.get("Retry-After")
                delay = int(retry_after) if retry_after and str(retry_after).isdigit() else wait
                logger.warning("B2 mint unavailable (attempt %s); waiting %ss", attempt, delay)
                self._sleeper(delay)
                continue
            break
        raise B2Error(
            (last or {}).get("error") or B2_UPLOAD_URL_UNAVAILABLE,
            B2_UPLOAD_URL_UNAVAILABLE,
        )

    def _post_b2_with_retry(self, drawing_id: str, hint: dict[str, Any] | None, payload: bytes) -> dict[str, Any]:
        current = hint
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                parsed = parse_upload_hint(current)
                return self._b2_post(
                    parsed,
                    payload,
                    content_type="application/pdf",
                    timeout=self.settings.upload_timeout,
                )
            except B2Error as exc:
                last_error = exc
                if exc.code == S3_FALLBACK_FORBIDDEN:
                    raise
                if attempt >= 3:
                    raise
                logger.warning("B2 POST failed (%s); minting a new URL", exc)
                current = self._mint_upload(drawing_id)
        raise last_error or B2Error("B2 upload failed")

    def _upload_drawing_ingest_key(self, project_id: str, item, named: DrawingName, label: str) -> dict[str, Any]:
        metadata = {
            "project_id": project_id,
            "filename": item.path.name,
            "relative_path": item.relative_path,
            "sheet_number": named.sheet_number,
            "sheet_title": named.sheet_title,
            "discipline": named.discipline,
            "drawing_set": named.drawing_set or label,
            "revision": named.revision,
            "split_pages": False,
            "source": ISSUE_SOURCE,
            "source_id": item.relative_path,
            "content_hash": file_sha256(item.path),
        }
        response = self.session.post(
            self._url("api/drawings"),
            files={"file": (item.path.name, item.path.read_bytes())},
            data={"metadata": json.dumps(metadata), "sourceSystem": ISSUE_SOURCE},
            timeout=max(self.timeout, 180),
        )
        payload = _json(response)
        if not response.ok and response.status_code != 201:
            raise UsiscmError(payload.get("error") or f"ingest drawing failed ({response.status_code})")
        return {
            "endpoint": "ingest/drawings",
            "filename": item.path.name,
            "imported": int(payload.get("count") or 1),
            "errors": [],
        }

    def _upload_document(
        self,
        project: dict[str, Any],
        item,
        category: FileCategory,
        label: str,
        *,
        batch_id: str,
    ) -> dict[str, Any]:
        self._ensure_auth()
        project_id = str(project.get("id") or project.get("project_id") or "")
        try:
            if self.settings.ingest_api_key:
                return self._upload_document_ingest_key(project_id, item, category, label)
            return self._upload_document_session(project_id, item, category, label, batch_id=batch_id)
        except (UsiscmError, OSError) as exc:
            self.report_issue(
                message=str(exc),
                relative_path=item.relative_path,
                filename=item.path.name,
                kind="document",
                batch_id=batch_id,
                project_id=project_id,
                project_number=_project_number(project),
                detail={"category": category.value},
            )
            return {
                "endpoint": "ingest/documents",
                "filename": item.path.name,
                "imported": 0,
                "error": str(exc),
            }

    def _upload_document_ingest_key(self, project_id: str, item, category: FileCategory, label: str) -> dict[str, Any]:
        session = self.ingest_session if self.uses_microsoft else self.session
        metadata = {
            "project_id": project_id,
            "filename": item.path.name,
            "relative_path": item.relative_path,
            "document_type": category.document_type,
            "title": item.path.stem,
            "source": ISSUE_SOURCE,
            "source_id": item.relative_path,
            "folder_name": label,
            "content_hash": file_sha256(item.path),
        }
        response = session.post(
            self._url("api/documents"),
            files={"file": (item.path.name, item.path.read_bytes())},
            data={"metadata": json.dumps(metadata), "sourceSystem": ISSUE_SOURCE},
            timeout=max(self.timeout, 180),
        )
        payload = _json(response)
        if response.ok or response.status_code == 201:
            return {"endpoint": "ingest/documents", "filename": item.path.name, "imported": 1, "errors": []}
        raise UsiscmError(payload.get("error") or f"document upload failed ({response.status_code})")

    def _upload_document_session(
        self,
        project_id: str,
        item,
        category: FileCategory,
        label: str,
        *,
        batch_id: str,
    ) -> dict[str, Any]:
        metadata = {
            "project_id": project_id,
            "filename": item.path.name,
            "relative_path": item.relative_path,
            "document_type": category.document_type,
            "title": item.path.stem,
            "source": ISSUE_SOURCE,
            "source_id": item.relative_path,
            "folder_name": label,
            "batch_id": batch_id,
            "kind": "document",
            "content_hash": file_sha256(item.path),
        }
        response = self.session.post(
            self._url("api/v1/ingest/files"),
            files={"file": (item.path.name, item.path.read_bytes())},
            data={
                "metadata": json.dumps(metadata),
                "kind": "document",
                "batch_id": batch_id,
                "content_hash": metadata["content_hash"],
            },
            timeout=max(self.timeout, 180),
        )
        payload = _json(response)
        if response.ok or response.status_code == 201:
            return {"endpoint": "v1/ingest/files", "filename": item.path.name, "imported": 1, "errors": []}
        raise UsiscmError(payload.get("error") or f"document upload failed ({response.status_code})")

    def _ensure_auth(self) -> None:
        if not self.token:
            self.login(interactive=False)
            if not self.token:
                raise UsiscmError(UNATTENDED_HINT)

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))


def _json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {"data": data}
    except ValueError:
        return {"error": response.text[:500]}


def _normalize_name(value: str) -> list[str]:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in value)
    return [part for part in cleaned.split() if part]


def _project_name(project: dict[str, Any]) -> str:
    return str(project.get("name") or project.get("project_name") or "")


def _project_number(project: dict[str, Any]) -> str:
    return str(project.get("number") or project.get("project_number") or "")


def _specialty_project_key(project: dict[str, Any], manifest: PackageManifest) -> str:
    """Prefer an explicit key, then the project number, then the display name."""
    for key in ("project_key", "number", "project_number", "name", "project_name"):
        value = project.get(key)
        if value:
            return str(value)
    return str(manifest.label or "")


def _estimate_id(project: dict[str, Any]) -> str | None:
    for key in ("estimate_id", "estimateId"):
        value = project.get(key)
        if value:
            return str(value)
    return None


def _job_id(project: dict[str, Any]) -> str:
    for key in ("job_id", "jobId"):
        if project.get(key):
            return str(project[key])
    if str(project.get("kind") or "").lower() == "job":
        return str(project.get("id") or project.get("project_id") or "")
    return str(project.get("id") or project.get("project_id") or "")


def _best_project_match(label: str, projects: list[dict[str, Any]]) -> dict[str, Any] | None:
    label_tokens = set(_normalize_name(label))
    if not label_tokens:
        return None

    ranked: list[tuple[float, dict[str, Any]]] = []
    for project in projects:
        name = _project_name(project)
        number = _project_number(project)
        name_tokens = set(_normalize_name(name))
        if number and number.lower() in label.lower():
            ranked.append((1.0, project))
            continue
        if not name_tokens:
            continue
        overlap = len(label_tokens & name_tokens) / max(len(label_tokens), 1)
        if overlap >= 0.5 or (label.lower() in name.lower()) or (name.lower() in label.lower()):
            ranked.append((overlap, project))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]
