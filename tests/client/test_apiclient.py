from unittest.mock import patch

from httpx import MockTransport, Response
import pytest

from cli115.client.general.base import APIClient
from cli115.exceptions import APIError


class TestCheckResponse:

    @pytest.mark.parametrize("data", [[], None, "ok"])
    def test_non_object_json(self, data):
        with APIClient() as client:
            client._check_response(Response(200, json=data))

    @pytest.mark.parametrize("data", [
        {"state": False, "error": "login expired"},
        {"state": 0, "errno": "0", "error": "login expired"},
    ])
    def test_failure_without_error_code(self, data):
        with APIClient() as client, pytest.raises(APIError, match="login expired"):
            client._check_response(Response(200, json=data))

    def test_string_error_code(self):
        with APIClient() as client, pytest.raises(FileNotFoundError):
            client._check_response(Response(200, json={"errno": "990002"}))

    def test_decrypted_error_is_checked(self):
        transport = MockTransport(lambda request: Response(
            200, json={"state": True, "data": "encrypted"}
        ))
        with (
            APIClient(transport=transport) as client,
            patch("cli115.client.general.base.rsa_encrypt", return_value=b"payload"),
            patch(
                "cli115.client.general.base.rsa_decrypt",
                return_value=b'{"state":false,"errno":20004}',
            ),
            pytest.raises(FileExistsError),
        ):
            client.post_encrypted("https://example.com/api", data={})

    def test_returns_resp_when_state_true(self):
        resp = Response(200, json={"state": True, "data": []})
        APIClient()._check_response(resp)

    def test_returns_resp_when_state_one(self):
        resp = Response(200, json={"state": 1, "data": []})
        APIClient()._check_response(resp)

    def test_raises_not_found_for_errno_990002(self):
        resp = Response(
            200, json={"state": False, "errno": 990002, "error": "not found"}
        )
        with pytest.raises(FileNotFoundError):
            APIClient()._check_response(resp)

    def test_raises_already_exists_for_errno_20004(self):
        resp = Response(200, json={"state": False, "errno": 20004, "error": "exists"})
        with pytest.raises(FileExistsError):
            APIClient()._check_response(resp)

    def test_raises_api_error_for_other_errno(self):
        resp = Response(
            200, json={"state": False, "errno": 99999, "error": "something"}
        )
        with pytest.raises(APIError) as exc_info:
            APIClient()._check_response(resp)
        assert exc_info.value.errno == 99999

    def test_uses_errNo_field(self):
        resp = Response(
            200, json={"state": False, "errNo": 990002, "error": "not found"}
        )
        with pytest.raises(FileNotFoundError):
            APIClient()._check_response(resp)

    def test_default_message_when_no_error_field(self):
        resp = Response(200, json={"state": False, "errno": 1})
        with pytest.raises(APIError) as exc_info:
            APIClient()._check_response(resp)
        assert "Unknown API error" in str(exc_info.value)
