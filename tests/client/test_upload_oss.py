import base64
from datetime import datetime, timezone
import hashlib
import io
from types import SimpleNamespace
from urllib.parse import parse_qs
from unittest.mock import MagicMock
import xml.etree.ElementTree as ET

import httpcore
import httpx
import pytest

from cli115.client.general import upload as upload_module
from cli115.client.general.base import APIClient
from cli115.client.general.upload import UploadClient


class TestMultipartUpload:
    @pytest.fixture
    def oss(self, monkeypatch):
        state = SimpleNamespace(
            now=1_700_000_000.0, age=0.0, tokens=[], token_calls=[], events=[],
            parts=[], complete_parts=[], errors={}, on_part=lambda: None,
            response_md5=None,
        )
        monkeypatch.setattr(upload_module.time, "time", lambda: state.now)
        monkeypatch.setattr(upload_module.time, "monotonic", lambda: state.age)

        def get_token(request):
            assert request.url.path == "/3.0/gettoken.php"
            index = len(state.token_calls)
            token = state.tokens[index] if index < len(state.tokens) else {
                "SecurityToken": f"token-{index}",
                "AccessKeyId": f"access-{index}",
                "AccessKeySecret": f"secret-{index}",
            }
            state.token_calls.append(token)
            return httpx.Response(200, json=token)

        def control(request):
            action = "init" if "uploads" in request.url.params else "complete"
            state.events.append((action, request.headers["x-oss-security-token"]))
            errors = state.errors.get(action, [])
            if errors:
                error = errors.pop(0)
                if isinstance(error, Exception):
                    raise error
                if isinstance(error, bytes):
                    return httpx.Response(403, content=error)
                return httpx.Response(
                    403, content=f"<Error><Code>{error}</Code></Error>"
                )
            if action == "init":
                return httpx.Response(
                    200, content="<Result><UploadId>session</UploadId></Result>"
                )
            state.complete_parts.append(ET.fromstring(request.content))
            return httpx.Response(200, json={"state": True, "data": {"file_id": "1"}})

        def part(request):
            params = parse_qs(request.url.target.decode().split("?", 1)[1])
            number = int(params["partNumber"][0])
            headers = dict(request.headers)
            data = b"".join(request.stream)
            assert headers[b"content-length"] == str(len(data)).encode()
            assert b"transfer-encoding" not in headers
            state.events.append((number, headers[b"x-oss-security-token"].decode()))
            state.parts.append((number, data, headers[b"authorization"]))
            state.on_part()
            errors = state.errors.get(number, [])
            if errors:
                error = errors.pop(0)
                body = error if isinstance(error, bytes) else (
                    f"<Error><Code>{error}</Code></Error>".encode()
                )
                return httpcore.Response(403, content=body)
            return httpcore.Response(200, headers={
                "ETag": f'"etag-{number}"',
                "Content-MD5": state.response_md5 or base64.b64encode(
                    hashlib.md5(data).digest()
                ),
            })

        pool = MagicMock()
        pool.__enter__.return_value = pool
        pool.handle_request.side_effect = part
        monkeypatch.setattr(
            upload_module.httpcore, "ConnectionPool", MagicMock(return_value=pool)
        )
        api = APIClient(transport=httpx.MockTransport(get_token))
        uploader = UploadClient(api)
        uploader._client.close()
        uploader._client = httpx.Client(transport=httpx.MockTransport(control))
        state.uploader = uploader
        yield state
        uploader._client.close()
        api.close()

    def upload(self, oss, file=None, part_size=4):
        return oss.uploader.multipart_upload(
            file if file is not None else io.BytesIO(b"abcdefghi"),
            bucket="bucket", object="object", part_size=part_size,
            callback={"callback": "{}", "callback_var": "{}"},
        )

    def test_parts_and_final_short_part(self, oss):
        result = self.upload(oss)
        assert result["data"]["file_id"] == "1"
        assert [(number, data) for number, data, _ in oss.parts] == [
            (1, b"abcd"), (2, b"efgh"), (3, b"i"),
        ]
        assert len(oss.token_calls) == 1
        assert [p.findtext("PartNumber") for p in oss.complete_parts[0]] == [
            "1", "2", "3",
        ]
        assert [p.findtext("ETag") for p in oss.complete_parts[0]] == [
            '"etag-1"', '"etag-2"', '"etag-3"',
        ]

    def test_short_reads_fill_parts(self, oss):
        class ShortReader(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 2))

        self.upload(oss, file=ShortReader(b"abcdefghi"))
        assert [data for _, data, _ in oss.parts] == [b"abcd", b"efgh", b"i"]

    def test_md5_mismatch_prevents_completion(self, oss):
        oss.response_md5 = b"wrong"
        with pytest.raises(RuntimeError, match="MD5 mismatch"):
            self.upload(oss)
        assert len(oss.parts) == 1
        assert oss.complete_parts == []

    @pytest.mark.parametrize("action", ["init", 2, "complete"])
    def test_expired_token_refreshes_and_retries_current_request(self, oss, action):
        oss.errors[action] = ["SecurityTokenExpired"]
        self.upload(oss)
        assert len(oss.token_calls) == 2
        attempts = [token for event, token in oss.events if event == action]
        assert attempts == ["token-0", "token-1"]
        if action == 2:
            assert [(number, data) for number, data, _ in oss.parts] == [
                (1, b"abcd"), (2, b"efgh"), (2, b"efgh"), (3, b"i"),
            ]
            assert oss.parts[1][2] != oss.parts[2][2]

    @pytest.mark.parametrize("action", ["init", 1, "complete"])
    @pytest.mark.parametrize("code", [
        "AccessDenied", "SignatureDoesNotMatch", b"<malformed",
    ])
    def test_other_403_errors_are_not_retried(self, oss, action, code):
        oss.errors[action] = [code]
        with pytest.raises((OSError, httpx.HTTPStatusError)):
            self.upload(oss)
        assert len(oss.token_calls) == 1
        assert sum(event == action for event, _ in oss.events) == 1

    @pytest.mark.parametrize("action", ["init", 1, "complete"])
    def test_repeated_expiration_has_bounded_retries(self, oss, action):
        oss.errors[action] = ["SecurityTokenExpired"] * 3
        with pytest.raises(OSError, match="SecurityTokenExpired"):
            self.upload(oss)
        assert len(oss.token_calls) == 3
        assert sum(event == action for event, _ in oss.events) == 3

    @pytest.mark.parametrize("expiration", [None, "invalid", "2030-01-01T00:00:00Z"])
    def test_token_age_refreshes_before_next_part(self, oss, expiration):
        oss.tokens = [{
            "SecurityToken": "first", "AccessKeyId": "access",
            "AccessKeySecret": "secret",
            "Expiration": expiration,
        }]
        oss.on_part = lambda: setattr(oss, "age", 901.0)
        self.upload(oss)
        assert len(oss.token_calls) == 2
        assert oss.events[:3] == [("init", "first"), (1, "first"), (2, "token-1")]

    def test_expiration_refreshes_before_completion(self, oss):
        expiration = datetime.fromtimestamp(oss.now + 120, timezone.utc).isoformat()
        oss.tokens = [{
            "SecurityToken": "first", "AccessKeyId": "access",
            "AccessKeySecret": "secret",
            "Expiration": expiration,
        }]
        def advance_time():
            oss.age += 61
            oss.now += 61
        oss.on_part = advance_time
        self.upload(oss, file=io.BytesIO(b"abcd"))
        assert oss.events == [("init", "first"), (1, "first"), ("complete", "token-1")]

    @pytest.mark.parametrize("error", [
        httpx.ConnectTimeout("handshake"), httpx.ConnectError("connect"),
    ])
    @pytest.mark.parametrize("action", ["init", "complete"])
    def test_control_connection_retries_without_new_token(self, oss, action, error):
        oss.errors[action] = [error]
        self.upload(oss)
        assert len(oss.token_calls) == 1
        assert sum(event == action for event, _ in oss.events) == 2

    def test_read_timeout_is_not_retried(self, oss):
        oss.errors["complete"] = [httpx.ReadTimeout("read")]
        with pytest.raises(httpx.ReadTimeout):
            self.upload(oss)
        assert sum(event == "complete" for event, _ in oss.events) == 1

    @pytest.mark.parametrize("part_size", [0, -1])
    def test_invalid_part_size_fails_before_network(self, oss, part_size):
        with pytest.raises(ValueError, match="part_size must be positive"):
            self.upload(oss, part_size=part_size)
        assert oss.token_calls == []


class TestUploadInit:
    def test_encrypted_response_is_decompressed(self):
        # AES-CBC encrypted length-prefixed LZ4 literal block for {"status":2}.
        cipher = bytes.fromhex("86c64b9b6b329e49c6ad73d7c03503a5")
        payload = {
            "filename": "file.bin", "fileid": "A" * 40, "filesize": 4,
            "target": "U_1_0", "userid": 1, "userkey": "key",
            "sign_key": "", "sign_val": "", "appversion": "30.7.2.1",
        }
        def respond(request):
            assert request.url.params.get("k_ec")
            assert request.content
            return httpx.Response(200, content=cipher)
        with APIClient(transport=httpx.MockTransport(respond)) as api:
            uploader = UploadClient(api)
            try:
                assert uploader._post_upload_init(payload) == {"status": 2}
                assert "t" not in payload
            finally:
                uploader._client.close()
