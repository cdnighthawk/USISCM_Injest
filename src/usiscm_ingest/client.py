"""HTTP client for USIS Construction Management (Microsoft-authenticated)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import requests

from usiscm_ingest.classify import FileCategory
from usiscm_ingest.config import Settings
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


class UsiscmError(RuntimeError):
    pass


@dataclass
class UploadResult:
    project_id: str
    imported: int = 0
    skipped: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "imported": self.imported,
            "skipped": self.skipped,
            "errors": self.errors,
            "details": self.details,
        }


class UsiscmClient:
    def __init__(self, settings: Settings, timeout: int = 120) -> None:
        self.settings = settings
        self.base_url = settings.base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.session = requests.Session()
        self.token: str | None = None
        self.auth_mode: str | None = None
        self._entra: EntraApp | None = None

    @property
    def uses_ingest_key(self) -> bool:
        return bool(self.settings.ingest_api_key)

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
        if self.settings.ingest_api_key:
            self.token = self.settings.ingest_api_key
            self.auth_mode = "ingest_key"
            self.session.headers["Authorization"] = f"Bearer {self.token}"
            return self.token
        try:
            token = resolve_access_token(
                self.entra_app(),
                token_path=self.settings.token_path,
                access_token=self.settings.ms_access_token or None,
                interactive=interactive,
            )
        except MicrosoftAuthError as exc:
            raise UsiscmError(str(exc)) from exc
        self.token = token
        self.auth_mode = "microsoft"
        self.session.headers["Authorization"] = f"Bearer {token}"
        return token

    def auth_status(self) -> dict[str, Any]:
        self._ensure_auth()
        response = self.session.get(self._url("api/v1/auth/status"), timeout=self.timeout)
        payload = _json(response)
        if not response.ok:
            raise UsiscmError(payload.get("error") or f"Auth status failed ({response.status_code})")
        return payload

    def list_projects(self, *, limit: int = 500, query: str = "") -> list[dict[str, Any]]:
        self._ensure_auth()
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
            response = self.session.get(
                self._url("api/v1/projects"),
                params={"limit": limit, "offset": offset},
                timeout=self.timeout,
            )
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
            if self.uses_ingest_key:
                matches = self.list_projects(query=project_id)
                for project in matches:
                    if str(project.get("id") or project.get("project_id")) == str(project_id):
                        return project
                raise UsiscmError(f"Project {project_id} not found")
            response = self.session.get(self._url(f"api/v1/projects/{project_id}"), timeout=self.timeout)
            payload = _json(response)
            if not response.ok:
                raise UsiscmError(payload.get("error") or f"Project {project_id} not found")
            return payload.get("item") or payload.get("data") or payload

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
    ) -> UploadResult:
        project = self.resolve_project(project_id=project_id, project_name=manifest.label)
        resolved_id = str(project.get("id") or project.get("project_id") or project_id or "")
        result = UploadResult(project_id=resolved_id)
        if dry_run:
            result.details.append(
                {
                    "dry_run": True,
                    "project": project.get("name") or project.get("project_name"),
                    "counts": manifest.counts,
                }
            )
            result.imported = len(manifest.files)
            return result

        drawings = [
            item
            for item in manifest.files_for(FileCategory.DRAWING)
            if item.path.suffix.lower() in DRAWING_EXTENSIONS
        ]
        for item in drawings:
            result.details.append(self._upload_drawing(resolved_id, item, manifest.label))

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
                if (
                    category == FileCategory.SPEC
                    and item.path.suffix.lower() == ".pdf"
                    and not self.uses_ingest_key
                ):
                    result.details.append(self._upload_spec_book(resolved_id, item))
                result.details.append(self._upload_document(resolved_id, item, category, manifest.label))

        for detail in result.details:
            result.imported += int(detail.get("imported") or 0)
            if detail.get("error"):
                result.errors.append({"filename": detail.get("filename"), "error": detail["error"]})
            result.errors.extend(detail.get("errors") or [])
        return result

    def _upload_drawing(self, project_id: str, item, label: str) -> dict[str, Any]:
        self._ensure_auth()
        if self.uses_ingest_key:
            metadata = {
                "project_id": project_id,
                "filename": item.path.name,
                "sheet_number": item.sheet_number,
                "drawing_set": label,
                "split_pages": item.path.suffix.lower() == ".pdf",
                "source": "usiscm_ingest",
                "source_id": item.relative_path,
            }
            response = self.session.post(
                self._url("api/drawings"),
                files={"file": (item.path.name, item.path.read_bytes())},
                data={"metadata": json.dumps(metadata), "sourceSystem": "usiscm_ingest"},
                timeout=max(self.timeout, 180),
            )
            payload = _json(response)
            if not response.ok and response.status_code != 201:
                return {"endpoint": "ingest/drawings", "filename": item.path.name, "imported": 0, "error": payload.get("error") or response.status_code}
            return {"endpoint": "ingest/drawings", "filename": item.path.name, "imported": int(payload.get("count") or 1), "errors": []}

        data = {
            "drawing_set": label,
            "split_pages": "1" if item.path.suffix.lower() == ".pdf" else "0",
        }
        if item.sheet_number:
            data["sheet_number"] = item.sheet_number
        response = self.session.post(
            self._url(f"api/v1/projects/{project_id}/drawings"),
            files={"file": (item.path.name, item.path.read_bytes())},
            data=data,
            timeout=max(self.timeout, 180),
        )
        payload = _json(response)
        if not response.ok:
            return {"endpoint": "v1/drawings", "filename": item.path.name, "imported": 0, "error": payload.get("error") or response.status_code}
        count = payload.get("count") or (len(payload.get("items") or []) or 1)
        return {"endpoint": "v1/drawings", "filename": item.path.name, "imported": int(count), "errors": []}

    def _upload_spec_book(self, project_id: str, item) -> dict[str, Any]:
        self._ensure_auth()
        if self.uses_ingest_key:
            return {"endpoint": "ingest/skip-spec-book", "filename": item.path.name, "imported": 0, "errors": []}
        response = self.session.post(
            self._url(f"api/v1/projects/{project_id}/spec-book/import"),
            files={"file": (item.path.name, item.path.read_bytes(), "application/pdf")},
            timeout=max(self.timeout, 180),
        )
        payload = _json(response)
        if not response.ok:
            return {
                "endpoint": "v1/spec-book/import",
                "filename": item.path.name,
                "imported": 0,
                "error": payload.get("error") or response.status_code,
            }
        return {"endpoint": "v1/spec-book/import", "filename": item.path.name, "imported": 1, "errors": []}

    def _upload_document(self, project_id: str, item, category: FileCategory, label: str) -> dict[str, Any]:
        self._ensure_auth()
        metadata = {
            "project_id": project_id,
            "filename": item.path.name,
            "document_type": category.document_type,
            "title": item.path.stem,
            "source": "usiscm_ingest",
            "source_id": item.relative_path,
            "folder_name": label,
        }
        response = self.session.post(
            self._url("api/documents"),
            files={"file": (item.path.name, item.path.read_bytes())},
            data={"metadata": json.dumps(metadata), "sourceSystem": "usiscm_ingest"},
            timeout=max(self.timeout, 180),
        )
        payload = _json(response)
        if response.ok or response.status_code == 201:
            return {"endpoint": "ingest/documents", "filename": item.path.name, "imported": 1, "errors": []}
        # Official ingest route is API-key gated. Fall back so a Microsoft
        # session can still land the file on the project.
        if item.path.suffix.lower() == ".pdf":
            fallback = self._upload_drawing(project_id, item, f"{label} / {category.value}")
            fallback["endpoint"] = "v1/drawings (document fallback)"
            return fallback
        return {
            "endpoint": "ingest/documents",
            "filename": item.path.name,
            "imported": 0,
            "error": payload.get("error") or response.status_code,
        }

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
