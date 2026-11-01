from unittest.mock import MagicMock, patch

import httpx

from cli115.client import create_client
from cli115.client.base import DownloadUrl, RemoteFile
from cli115.client.general.base import APIClient
from cli115.client.general.upload import UploadClient
from tests.client.conftest import make_client


def _make_info(**kwargs):
    defaults = dict(
        url="https://example.com/file.bin",
        file_name="file.bin",
        file_size=1024,
        sha1="A" * 40,
        user_agent="TestAgent/1.0",
        referer="https://115.com/",
        cookies="UID=test",
    )
    defaults.update(kwargs)
    return DownloadUrl(**defaults)


def _make_auth():
    auth = MagicMock()
    auth.get_cookies.return_value = {}
    return auth


class TestCreateClientProxy:

    def test_no_proxy_default(self):
        client = create_client(_make_auth())
        try:
            assert client._file._api._mounts == {}
            assert client._file._uploader._client._mounts == {}
        finally:
            client._file._api.close()
            client._file._uploader._client.close()

    def test_proxy_applies_to_api_client(self):
        client = create_client(_make_auth(), proxy="http://127.0.0.1:7890")
        try:
            mounts = client._file._api._mounts
            assert len(mounts) == 1
            pat = next(iter(mounts))
            assert pat.matches(httpx.URL("http://115.com"))
            assert pat.matches(httpx.URL("https://115.com"))
        finally:
            client._file._api.close()
            client._file._uploader._client.close()

    def test_socks5_proxy(self):
        client = create_client(_make_auth(), proxy="socks5://127.0.0.1:1080")
        try:
            mounts = client._file._api._mounts
            assert len(mounts) == 1
            pat = next(iter(mounts))
            assert pat.matches(httpx.URL("http://115.com"))
            assert pat.matches(httpx.URL("https://115.com"))
        finally:
            client._file._api.close()
            client._file._uploader._client.close()

    def test_transport_takes_precedence(self):
        mock_transport = httpx.MockTransport(lambda r: httpx.Response(200))
        client = create_client(
            _make_auth(),
            transport=mock_transport,
            proxy="http://127.0.0.1:7890",
        )
        try:
            assert client._file._api._mounts == {}
            assert client._file._api._transport is mock_transport
        finally:
            client._file._api.close()
            client._file._uploader._client.close()

    def test_upload_client_gets_proxy(self):
        client = create_client(_make_auth(), proxy="http://127.0.0.1:7890")
        try:
            assert client._file._uploader._proxy == "http://127.0.0.1:7890"
            mounts = client._file._uploader._client._mounts
            assert len(mounts) == 1
            pat = next(iter(mounts))
            assert pat.matches(httpx.URL("http://115.com"))
            assert pat.matches(httpx.URL("https://115.com"))
        finally:
            client._file._api.close()
            client._file._uploader._client.close()


class TestUploadPoolProxy:

    def test_pool_uses_proxy_when_set(self):
        api = APIClient(transport=httpx.MockTransport(lambda request: None))
        uploader = UploadClient(api, proxy="http://127.0.0.1:7890")
        try:
            pool = uploader._make_pool()
            assert pool._proxy is not None
            assert pool._proxy.url.scheme == b"http"
            pool.close()
        finally:
            api.close()
            uploader._client.close()

    def test_pool_has_no_proxy_by_default(self):
        api = APIClient(transport=httpx.MockTransport(lambda request: None))
        uploader = UploadClient(api)
        try:
            pool = uploader._make_pool()
            assert pool._proxy is None
            pool.close()
        finally:
            api.close()
            uploader._client.close()


class TestRemoteFileProxy:

    def test_remote_file_passes_proxy_to_httpx_client(self):
        info = _make_info()
        rf = RemoteFile(info, proxy="http://127.0.0.1:7890")
        with patch("httpx.Client") as mock_cls:
            rf._ensure_client()
            mock_cls.assert_called_once_with(
                headers={
                    "User-Agent": info.user_agent,
                    "Cookie": info.cookies,
                    "Referer": info.referer,
                },
                follow_redirects=True,
                proxy="http://127.0.0.1:7890",
            )

    def test_file_client_open_passes_proxy(self):
        client = make_client()
        client.file._proxy = "http://127.0.0.1:7890"
        with patch.object(client.file, "url", return_value=_make_info()), \
             patch("cli115.client.base.RemoteFile") as mock_rf:
            client.file.open("/test.txt")
            assert mock_rf.call_args.kwargs["proxy"] == "http://127.0.0.1:7890"
