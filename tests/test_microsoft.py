import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from usiscm_ingest.microsoft import (
    EntraApp,
    MicrosoftAuthError,
    MicrosoftTokens,
    device_code_login,
    discover_entra_app,
    load_tokens,
    refresh_tokens,
    save_tokens,
    tokens_from_endpoint,
)


def test_discover_entra_app_parses_microsoft_redirect() -> None:
    response = MagicMock()
    response.headers = {
        "Location": (
            "https://login.microsoftonline.com/41f6feda-3925-4a7f-801d-44b91cd604a1"
            "/oauth2/v2.0/authorize?client_id=738dce41-ed61-4475-82ae-5800963231c0"
            "&scope=openid%20profile%20email%20offline_access"
        )
    }
    with patch("usiscm_ingest.microsoft.requests.get", return_value=response):
        app = discover_entra_app("https://www.usiscm.com")
    assert app.tenant_id == "41f6feda-3925-4a7f-801d-44b91cd604a1"
    assert app.client_id == "738dce41-ed61-4475-82ae-5800963231c0"
    assert "User.Read" in app.scopes


def test_discover_entra_app_rejects_non_microsoft_redirect() -> None:
    response = MagicMock()
    response.headers = {"Location": "https://www.usiscm.com/page-login.html?ms_error=not_configured"}
    with patch("usiscm_ingest.microsoft.requests.get", return_value=response):
        with pytest.raises(MicrosoftAuthError):
            discover_entra_app("https://www.usiscm.com")


def test_device_code_login_polls_until_token() -> None:
    app = EntraApp("tenant", "client")
    pending = MagicMock()
    pending.ok = False
    pending.json.return_value = {"error": "authorization_pending"}
    done = MagicMock()
    done.ok = True
    done.json.return_value = {"access_token": "atok", "refresh_token": "rtok", "expires_in": 60}

    start = MagicMock()
    start.ok = True
    start.json.return_value = {
        "device_code": "dev",
        "user_code": "ABCD",
        "verification_uri": "https://microsoft.com/devicelogin",
        "expires_in": 90,
        "interval": 1,
        "message": "enter ABCD",
    }

    with patch("usiscm_ingest.microsoft.requests.post", side_effect=[start, pending, done]):
        tokens = device_code_login(app, announce=lambda _: None, sleeper=lambda _: None)
    assert tokens.access_token == "atok"
    assert tokens.refresh_token == "rtok"


def test_refresh_and_cache_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ms_tokens.json"
    save_tokens(path, MicrosoftTokens(access_token="a", refresh_token="r", expires_at=time.time() + 10))
    loaded = load_tokens(path)
    assert loaded is not None
    assert loaded.access_token == "a"

    response = MagicMock()
    response.ok = True
    response.json.return_value = {"access_token": "a2", "expires_in": 30}
    with patch("usiscm_ingest.microsoft.requests.post", return_value=response):
        refreshed = refresh_tokens(EntraApp("t", "c"), "r")
    assert refreshed.access_token == "a2"
    assert refreshed.refresh_token == "r"


def test_tokens_from_endpoint_requires_access_token() -> None:
    with pytest.raises(MicrosoftAuthError):
        tokens_from_endpoint({"error_description": "nope"})
