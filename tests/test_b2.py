from usiscm_ingest.b2 import (
    B2Error,
    S3_FALLBACK_FORBIDDEN,
    is_native_b2_upload_url,
    parse_upload_hint,
    post_file,
    sha1_hex,
)


def test_native_url_accepted() -> None:
    url = "https://pod-000-1001-00.backblaze.com/b2api/v2/b2_upload_file/4_bucket/tok"
    assert is_native_b2_upload_url(url)


def test_s3_url_rejected() -> None:
    url = "https://s3.us-west-004.backblazeb2.com/bucket/key.pdf?X-Amz-Credential=x&X-Amz-Signature=x"
    assert not is_native_b2_upload_url(url)
    try:
        parse_upload_hint({"mode": "b2_native", "url": url, "authorization": "tok"})
        raise AssertionError("expected B2Error")
    except B2Error as exc:
        assert exc.code == S3_FALLBACK_FORBIDDEN


def test_s3_protocol_rejected_even_with_native_looking_host() -> None:
    try:
        parse_upload_hint(
            {
                "protocol": "s3_presigned_put",
                "uploadUrl": "https://example.com/put",
                "authorizationToken": "tok",
            }
        )
        raise AssertionError("expected B2Error")
    except B2Error as extra:
        assert extra.code == S3_FALLBACK_FORBIDDEN


def test_parse_accepts_desktop_locked_shape() -> None:
    hint = parse_upload_hint(
        {
            "protocol": "b2-native",
            "uploadUrl": "https://pod.backblaze.com/b2api/v2/b2_upload_file/x",
            "authorizationToken": "tok",
            "fileName": "jobs/1/drawings/a.pdf",
        }
    )
    assert hint["mode"] == "b2_native"
    assert hint["authorization"] == "tok"
    assert hint["file_name"].endswith("a.pdf")


def test_post_file_does_not_send_usiscm_bearer(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"fileId": "fid", "fileName": "a.pdf", "contentSha1": "dead"}

    class FakeSession:
        def post(self, url, data=None, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["data"] = data
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr("usiscm_ingest.b2.requests.Session", FakeSession)
    payload = b"%PDF-1.4"
    result = post_file(
        {
            "mode": "b2_native",
            "url": "https://pod.backblaze.com/b2api/v2/b2_upload_file/x",
            "authorization": "b2-only",
            "file_name": "drawings/a.pdf",
        },
        payload,
    )
    assert captured["headers"]["Authorization"] == "b2-only"
    assert "Bearer" not in captured["headers"]["Authorization"]
    assert captured["headers"]["X-Bz-Content-Sha1"] == sha1_hex(payload)
    assert result["fileId"] == "fid"
