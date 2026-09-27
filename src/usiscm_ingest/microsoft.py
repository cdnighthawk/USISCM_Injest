"""Microsoft Entra device-code login for USISCM.

The website uses ``/auth/microsoft/start``. The desktop/API path accepts the
same user's Microsoft access token as ``Authorization: Bearer``. This module
gets that token without an email/password form.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

MS_LOGIN = "https://login.microsoftonline.com"
DEFAULT_SCOPES = "openid profile email offline_access User.Read"
DEFAULT_TOKEN_PATH = Path.home() / ".config" / "usiscm-ingest" / "ms_tokens.json"
# Renew before expiry so a multi-hour sheet upload is not still using a token
# that will die during the next native B2 create/ack. Microsoft access tokens
# are often about an hour; the refresh token from `usiscm-ingest login` is what
# keeps a night job signed in.
REFRESH_SKEW_SECONDS = 15 * 60


UNATTENDED_HINT = (
    "Night jobs cannot wait for Microsoft sign-in. Set USISCM_INGEST_API_KEY "
    "on the server (recommended), or run `usiscm-ingest login` once during the "
    "day so a refresh token is saved."
)


class MicrosoftAuthError(RuntimeError):
    pass


@dataclass
class EntraApp:
    tenant_id: str
    client_id: str
    scopes: str = DEFAULT_SCOPES


@dataclass
class MicrosoftTokens:
    access_token: str
    refresh_token: str | None = None
    expires_at: float = 0
    token_type: str = "Bearer"

    def needs_refresh(self, *, now: float | None = None, skew: float = REFRESH_SKEW_SECONDS) -> bool:
        """True when this access token should be renewed before the next API call.

        ``expires_at <= 0`` means the TTL is unknown (for example an access token
        passed in from the environment). Those are not refreshed on every call;
        a 401 still forces one refresh when a refresh token is saved.
        """
        if self.expires_at <= 0:
            return False
        current = time.time() if now is None else now
        return current >= (self.expires_at - skew)

    @property
    def expired(self) -> bool:
        return self.needs_refresh()

    tenant_id: str = ""
    client_id: str = ""
    scopes: str = DEFAULT_SCOPES

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
            "tenant_id": self.tenant_id,
            "client_id": self.client_id,
            "scopes": self.scopes,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> MicrosoftTokens:
        return cls(
            access_token=str(raw.get("access_token") or ""),
            refresh_token=str(raw.get("refresh_token") or "") or None,
            expires_at=float(raw.get("expires_at") or 0),
            token_type=str(raw.get("token_type") or "Bearer"),
            tenant_id=str(raw.get("tenant_id") or ""),
            client_id=str(raw.get("client_id") or ""),
            scopes=str(raw.get("scopes") or DEFAULT_SCOPES),
        )


def discover_entra_app(base_url: str, timeout: int = 30) -> EntraApp:
    """Read tenant and client id from the live Microsoft SSO start redirect."""
    url = urljoin(base_url.rstrip("/") + "/", "auth/microsoft/start")
    response = requests.get(url, allow_redirects=False, timeout=timeout)
    location = response.headers.get("Location") or ""
    if "login.microsoftonline.com" not in location:
        raise MicrosoftAuthError(
            "USISCM did not redirect to Microsoft. Check USISCM_BASE_URL "
            f"({base_url}) and that Microsoft sign-in is enabled."
        )
    parsed = urlparse(location)
    parts = [p for p in parsed.path.split("/") if p]
    tenant = parts[0] if parts else ""
    client_id = (parse_qs(parsed.query).get("client_id") or [""])[0]
    if not tenant or not client_id:
        raise MicrosoftAuthError(f"Could not parse Microsoft app from redirect: {location}")
    scope = (parse_qs(parsed.query).get("scope") or [DEFAULT_SCOPES])[0]
    if "User.Read" not in scope:
        scope = f"{scope} User.Read".strip()
    return EntraApp(tenant_id=tenant, client_id=client_id, scopes=scope)


def load_tokens(path: Path) -> MicrosoftTokens | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not raw.get("access_token"):
        return None
    return MicrosoftTokens.from_dict(raw)


def save_tokens(path: Path, tokens: MicrosoftTokens) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens.to_dict(), indent=2), encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def clear_tokens(path: Path) -> None:
    if path.exists():
        path.unlink()


def tokens_from_endpoint(payload: dict[str, Any]) -> MicrosoftTokens:
    access = str(payload.get("access_token") or "")
    if not access:
        raise MicrosoftAuthError(payload.get("error_description") or "Microsoft did not return an access token")
    expires_in = int(payload.get("expires_in") or 3600)
    return MicrosoftTokens(
        access_token=access,
        refresh_token=str(payload.get("refresh_token") or "") or None,
        expires_at=time.time() + expires_in,
        token_type=str(payload.get("token_type") or "Bearer"),
    )


def refresh_tokens(app: EntraApp, refresh_token: str, timeout: int = 30) -> MicrosoftTokens:
    response = requests.post(
        f"{MS_LOGIN}/{app.tenant_id}/oauth2/v2.0/token",
        data={
            "client_id": app.client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": app.scopes,
        },
        timeout=timeout,
    )
    payload = _json(response)
    if not response.ok:
        raise MicrosoftAuthError(payload.get("error_description") or f"Microsoft refresh failed ({response.status_code})")
    tokens = tokens_from_endpoint(payload)
    if not tokens.refresh_token:
        tokens.refresh_token = refresh_token
    tokens.tenant_id = app.tenant_id
    tokens.client_id = app.client_id
    tokens.scopes = app.scopes
    return tokens


def device_code_login(
    app: EntraApp,
    *,
    timeout: int = 30,
    poll_seconds: int = 5,
    announce: Callable[[str], None] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> MicrosoftTokens:
    """Interactive Microsoft login for a server: print a code, user signs in on any device."""
    start = requests.post(
        f"{MS_LOGIN}/{app.tenant_id}/oauth2/v2.0/devicecode",
        data={"client_id": app.client_id, "scope": app.scopes},
        timeout=timeout,
    )
    body = _json(start)
    if not start.ok:
        raise MicrosoftAuthError(
            body.get("error_description")
            or (
                "Microsoft device login is not enabled for this app. "
                "Ask IT to allow the device-code flow, or set USISCM_MS_ACCESS_TOKEN."
            )
        )
    message = str(body.get("message") or "")
    uri = str(body.get("verification_uri") or "https://microsoft.com/devicelogin")
    user_code = str(body.get("user_code") or "")
    device_code = str(body.get("device_code") or "")
    expires_in = int(body.get("expires_in") or 900)
    interval = max(int(body.get("interval") or poll_seconds), 1)
    text = message or f"Open {uri} and enter code {user_code}"
    if announce:
        announce(text)
    else:
        print(text, flush=True)

    deadline = clock() + expires_in
    while clock() < deadline:
        sleeper(interval)
        token_res = requests.post(
            f"{MS_LOGIN}/{app.tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": app.client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
            },
            timeout=timeout,
        )
        payload = _json(token_res)
        error = str(payload.get("error") or "")
        if token_res.ok and payload.get("access_token"):
            tokens = tokens_from_endpoint(payload)
            tokens.tenant_id = app.tenant_id
            tokens.client_id = app.client_id
            tokens.scopes = app.scopes
            return tokens
        if error in {"authorization_pending", "slow_down"}:
            if error == "slow_down":
                interval += 2
            continue
        raise MicrosoftAuthError(payload.get("error_description") or error or "Microsoft login failed")
    raise MicrosoftAuthError("Microsoft login timed out. Run usiscm-ingest login again.")


def _require_app(app: EntraApp | None, app_factory: Callable[[], EntraApp] | None) -> EntraApp:
    if app is not None:
        return app
    if app_factory is not None:
        return app_factory()
    raise MicrosoftAuthError("Microsoft app is not configured")


def _with_supplied_access(cached: MicrosoftTokens | None, access_token: str) -> MicrosoftTokens:
    """Keep a saved refresh token when the caller passes only an access token."""
    if cached and cached.access_token == access_token:
        return cached
    if cached and cached.refresh_token:
        return MicrosoftTokens(
            access_token=access_token,
            refresh_token=cached.refresh_token,
            expires_at=0,
            tenant_id=cached.tenant_id,
            client_id=cached.client_id,
            scopes=cached.scopes or DEFAULT_SCOPES,
        )
    return MicrosoftTokens(access_token=access_token, expires_at=0)


def _saved_token_needs_login_refresh(tokens: MicrosoftTokens) -> bool:
    """Login-time check. Unknown expiry is renewed once, not on every request."""
    if not tokens.refresh_token:
        return False
    if tokens.expires_at <= 0:
        return True
    return tokens.needs_refresh()


def _try_refresh(
    app: EntraApp | None,
    app_factory: Callable[[], EntraApp] | None,
    tokens: MicrosoftTokens,
    token_path: Path,
    timeout: int,
) -> MicrosoftTokens | None:
    if not tokens.refresh_token:
        return None
    try:
        entra = _require_app(app, app_factory)
        refreshed = refresh_tokens(entra, tokens.refresh_token, timeout=timeout)
    except MicrosoftAuthError as exc:
        logger.warning("Microsoft refresh failed: %s", exc)
        return None
    save_tokens(token_path, refreshed)
    return refreshed


def resolve_microsoft_tokens(
    app: EntraApp | None = None,
    *,
    token_path: Path,
    access_token: str | None = None,
    interactive: bool = False,
    app_factory: Callable[[], EntraApp] | None = None,
    timeout: int = 30,
) -> MicrosoftTokens:
    """Return a Microsoft session, refreshing it when the access token is near expiry."""
    cached = load_tokens(token_path)
    if access_token:
        supplied = _with_supplied_access(cached, access_token)
        if _saved_token_needs_login_refresh(supplied):
            refreshed = _try_refresh(app, app_factory, supplied, token_path, timeout)
            if refreshed is not None:
                return refreshed
        return supplied

    if cached and cached.expires_at > 0 and not cached.needs_refresh():
        return cached
    if cached and _saved_token_needs_login_refresh(cached):
        refreshed = _try_refresh(app, app_factory, cached, token_path, timeout)
        if refreshed is not None:
            return refreshed
        # A known-dead access token cannot carry a night job. Unknown TTL with a
        # failed refresh falls through the same way.
    if not interactive:
        raise MicrosoftAuthError(UNATTENDED_HINT)
    entra = _require_app(app, app_factory)
    tokens = device_code_login(entra, timeout=timeout)
    tokens.tenant_id = entra.tenant_id
    tokens.client_id = entra.client_id
    tokens.scopes = entra.scopes
    save_tokens(token_path, tokens)
    if not tokens.refresh_token:
        logger.warning(
            "Microsoft did not return a refresh token. Night jobs will fail after this access token expires. "
            "Set USISCM_INGEST_API_KEY for unattended runs."
        )
    return tokens


def resolve_access_token(
    app: EntraApp,
    *,
    token_path: Path,
    access_token: str | None = None,
    interactive: bool = False,
) -> str:
    return resolve_microsoft_tokens(
        app,
        token_path=token_path,
        access_token=access_token,
        interactive=interactive,
    ).access_token


def _json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {"data": data}
    except ValueError:
        return {"error_description": response.text[:400]}
