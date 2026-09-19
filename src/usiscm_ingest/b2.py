"""Native Backblaze B2 upload — the same path USISPdfApp uses.

Bytes go office PC → B2. The website only mints a one-shot URL and stores
the ack. S3 presigned PUTs are refused; that is what used to 403 ingest.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger(__name__)

S3_FALLBACK_FORBIDDEN = "S3_FALLBACK_FORBIDDEN"
B2_UPLOAD_URL_UNAVAILABLE = "B2_UPLOAD_URL_UNAVAILABLE"


class B2Error(RuntimeError):
    def __init__(self, message: str, code: str = "B2_ERROR") -> None:
        super().__init__(message)
        self.code = code


def is_native_b2_upload_url(url: str | None) -> bool:
    raw = (url or "").strip()
    if not raw:
        return False
    lower = raw.lower()
    if "x-amz-" in lower:
        return False
    try:
        host = (urlparse(raw).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host.startswith("s3.") or ".s3." in host or host.endswith(".amazonaws.com"):
        return False
    return "b2_upload_file" in lower


def parse_upload_hint(payload: dict[str, Any] | None) -> dict[str, str]:
    """Normalize mint JSON from create-drawing / upload-session.

    Live Flask returns ``mode/url/authorization/file_name``. The locked
    desktop contract also uses ``protocol/uploadUrl/authorizationToken``.
    """
    raw = payload if isinstance(payload, dict) else {}
    nested = raw.get("upload") if isinstance(raw.get("upload"), dict) else raw
    url = str(
        nested.get("url")
        or nested.get("uploadUrl")
        or nested.get("upload_url")
        or ""
    ).strip()
    token = str(
        nested.get("authorization")
        or nested.get("authorizationToken")
        or nested.get("authorization_token")
        or ""
    ).strip()
    file_name = str(
        nested.get("file_name")
        or nested.get("fileName")
        or nested.get("object_name")
        or ""
    ).strip()
    mode = str(nested.get("mode") or nested.get("protocol") or "").strip().lower().replace("-", "_")
    if mode in {"s3", "s3_presigned_put", "presigned_put"} or "x-amz-" in url.lower():
        raise B2Error(
            "website handed back an S3 upload URL; native B2 only",
            S3_FALLBACK_FORBIDDEN,
        )
    if mode and mode not in {"b2_native", "b2native"}:
        raise B2Error(
            f"unsupported upload protocol {mode!r}",
            S3_FALLBACK_FORBIDDEN,
        )
    if not token or not is_native_b2_upload_url(url):
        raise B2Error(
            "native B2 upload URL missing or looked like S3",
            B2_UPLOAD_URL_UNAVAILABLE,
        )
    return {
        "mode": "b2_native",
        "url": url,
        "authorization": token,
        "file_name": file_name,
        "sha1_header": str(nested.get("sha1_header") or "X-Bz-Content-Sha1"),
    }


def sha1_hex(payload: bytes) -> str:
    return hashlib.sha1(payload).hexdigest()


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def post_file(
    hint: dict[str, str],
    payload: bytes,
    *,
    content_type: str = "application/pdf",
    timeout: int = 600,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """POST file bytes to B2. Dedicated client — no usiscm Authorization."""
    parsed = parse_upload_hint(hint)
    headers = {
        "Authorization": parsed["authorization"],
        "X-Bz-Content-Sha1": sha1_hex(payload),
        "Content-Type": content_type or "application/octet-stream",
        "Content-Length": str(len(payload)),
    }
    name = parsed["file_name"]
    if name:
        headers["X-Bz-File-Name"] = quote(name, safe="/")
    client = session or requests.Session()
    owns = session is None
    try:
        response = client.post(
            parsed["url"],
            data=payload,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise B2Error(f"B2 upload connection failed: {exc}") from exc
    finally:
        if owns:
            client.close()
    if response.status_code in {401, 403}:
        body = (response.text or "")[:240]
        if "x-amz-" in parsed["url"].lower() or "invalidaccesskeyid" in body.lower():
            raise B2Error("B2 rejected an S3-style upload", S3_FALLBACK_FORBIDDEN)
        raise B2Error(f"B2 upload unauthorized ({response.status_code})")
    if response.status_code >= 400:
        raise B2Error(f"B2 upload failed ({response.status_code}): {(response.text or '')[:240]}")
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "fileId": data.get("fileId") or data.get("file_id"),
        "fileName": data.get("fileName") or data.get("file_name") or name,
        "contentSha1": data.get("contentSha1") or data.get("content_sha1"),
        "contentLength": len(payload),
    }
