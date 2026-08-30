"""HTTP client for the USIS Construction Management API."""

from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

from usiscm_ingest.classify import FileCategory
from usiscm_ingest.package import PackageManifest

logger = logging.getLogger(__name__)

DRAWING_IMPORT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
DRAWING_TABLE_EXTENSIONS = {".dwg", ".dxf", ".pdf", ".png", ".jpg", ".jpeg", ".xls", ".xlsx"}
DOCUMENT_EXTENSIONS = {".pdf", ".dwg", ".doc", ".docx", ".xls", ".xlsx", ".png", ".jpg", ".jpeg"}


class UsiscmError(RuntimeError):
    pass


@dataclass
class UploadResult:
    project_id: int
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
    def __init__(self, base_url: str, email: str, password: str, timeout: int = 120) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.email = email
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.token: str | None = None

    def login(self) -> str:
        response = self.session.post(
            self._url("api/auth/login"),
            json={"identifier": self.email, "email": self.email, "password": self.password},
            timeout=self.timeout,
        )
        payload = _json(response)
        if not response.ok or not payload.get("success"):
            raise UsiscmError(payload.get("message") or payload.get("error") or f"Login failed ({response.status_code})")
        token = payload.get("token")
        if not token:
            raise UsiscmError("Login succeeded but no token was returned")
        self.token = token
        self.session.headers["Authorization"] = f"Bearer {token}"
        return token

    def list_projects(self, *, stage: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        self._ensure_auth()
        params: dict[str, Any] = {"limit": limit, "include_count": "true"}
        if stage:
            params["stage"] = stage
        collected: list[dict[str, Any]] = []
        offset = 0
        while True:
            params["offset"] = offset
            response = self.session.get(self._url("api/projects"), params=params, timeout=self.timeout)
            payload = _json(response)
            if not response.ok:
                raise UsiscmError(payload.get("error") or f"Project list failed ({response.status_code})")
            batch = payload.get("data") or []
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
        project_id: int | None = None,
        project_name: str | None = None,
    ) -> dict[str, Any]:
        if project_id is not None:
            self._ensure_auth()
            response = self.session.get(self._url(f"api/projects/{project_id}"), timeout=self.timeout)
            payload = _json(response)
            if not response.ok:
                raise UsiscmError(payload.get("error") or f"Project {project_id} not found")
            return payload.get("data") or payload

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
        project_id: int | None = None,
        dry_run: bool = False,
    ) -> UploadResult:
        project = self.resolve_project(project_id=project_id, project_name=manifest.label)
        resolved_id = int(project.get("id") or project_id)
        result = UploadResult(project_id=resolved_id)
        if dry_run:
            result.details.append({"dry_run": True, "project": project.get("project_name"), "counts": manifest.counts})
            result.imported = len(manifest.files)
            return result

        drawings = manifest.files_for(FileCategory.DRAWING)
        drawing_import = [f for f in drawings if f.path.suffix.lower() in DRAWING_IMPORT_EXTENSIONS]
        drawing_table = [f for f in drawings if f.path.suffix.lower() in DRAWING_TABLE_EXTENSIONS - DRAWING_IMPORT_EXTENSIONS]
        if drawing_import:
            result.details.append(self._import_drawing_set(resolved_id, drawing_import, manifest.label))
        if drawing_table:
            result.details.append(self._bulk_drawings(resolved_id, drawing_table))

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
            if docs:
                result.details.append(self._bulk_documents(resolved_id, docs, category))

        for detail in result.details:
            result.imported += int(detail.get("imported") or 0)
            result.errors.extend(detail.get("errors") or [])
        return result

    def _import_drawing_set(self, project_id: int, files: list, label: str) -> dict[str, Any]:
        self._ensure_auth()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in files:
                archive.write(item.path, arcname=item.path.name)
        buffer.seek(0)
        response = self.session.post(
            self._url(f"api/projects/{project_id}/drawings/import"),
            files={"file": (f"{label}-drawings.zip", buffer, "application/zip")},
            data={"drawing_set": label, "use_ai": "0"},
            timeout=max(self.timeout, 300),
        )
        payload = _json(response)
        if not response.ok and response.status_code != 207:
            logger.warning("drawings/import failed (%s); falling back to documents/bulk", response.status_code)
            return self._bulk_drawings(project_id, files)
        return {
            "endpoint": "drawings/import",
            "imported": payload.get("imported") or len(payload.get("sheets") or []),
            "skipped": payload.get("skipped") or 0,
            "errors": payload.get("errors") or [],
        }

    def _bulk_drawings(self, project_id: int, files: list) -> dict[str, Any]:
        self._ensure_auth()
        uploads = [("files", (item.path.name, item.path.read_bytes())) for item in files]
        response = self.session.post(
            self._url(f"api/documents/projects/{project_id}/bulk"),
            files=uploads,
            timeout=max(self.timeout, 180),
        )
        payload = _json(response)
        if not response.ok and response.status_code != 207:
            raise UsiscmError(payload.get("error") or f"Drawing bulk upload failed ({response.status_code})")
        return {
            "endpoint": "documents/bulk",
            "imported": payload.get("imported") or 0,
            "skipped": payload.get("skipped") or 0,
            "errors": payload.get("errors") or [],
        }

    def _bulk_documents(self, project_id: int, files: list, category: FileCategory) -> dict[str, Any]:
        self._ensure_auth()
        uploads = [("files", (item.path.name, item.path.read_bytes())) for item in files]
        response = self.session.post(
            self._url(f"api/documents/projects/{project_id}/documents/bulk-docs"),
            files=uploads,
            data={"category": category.document_category, "description": f"Ingested as {category.value}"},
            timeout=max(self.timeout, 180),
        )
        payload = _json(response)
        if not response.ok and response.status_code != 207:
            raise UsiscmError(payload.get("error") or f"Document bulk upload failed ({response.status_code})")
        return {
            "endpoint": "documents/bulk-docs",
            "category": category.value,
            "imported": payload.get("imported") or 0,
            "skipped": payload.get("skipped") or 0,
            "errors": payload.get("errors") or [],
        }

    def _ensure_auth(self) -> None:
        if not self.token:
            self.login()

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


def _best_project_match(label: str, projects: list[dict[str, Any]]) -> dict[str, Any] | None:
    label_tokens = set(_normalize_name(label))
    if not label_tokens:
        return None

    ranked: list[tuple[float, dict[str, Any]]] = []
    for project in projects:
        name = str(project.get("project_name") or "")
        number = str(project.get("project_number") or "")
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
