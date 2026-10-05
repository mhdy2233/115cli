from unittest.mock import MagicMock, patch

import httpx
import pytest

from cli115.client.base import DownloadUrl, FileClient, RemoteFile


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


class TestRemoteFile:
    def test_properties(self):
        info = _make_info()
        rf = RemoteFile(info)
        assert rf.name == "file.bin"
        assert rf.size == 1024
        assert rf.readable()
        assert not rf.writable()
        assert rf.seekable()
        assert rf.tell() == 0

    def test_stream_flag_defaults_false(self):
        rf = RemoteFile(_make_info())
        assert not rf._stream

    def test_set_stream_enables_and_disables(self):
        rf = RemoteFile(_make_info())
        rf.set_stream(True)
        assert rf._stream
        rf.set_stream(False)
        assert not rf._stream

    def test_seek(self):
        rf = RemoteFile(_make_info(file_size=100))
        assert rf.seek(50) == 50
        assert rf.tell() == 50
        assert rf.seek(10, 1) == 60
        assert rf.seek(-10, 2) == 90

    def test_read_eof_returns_empty(self):
        rf = RemoteFile(_make_info(file_size=5))
        rf.seek(5)
        assert rf.read() == b""

    def test_context_manager(self):
        rf = RemoteFile(_make_info())
        with rf as f:
            assert f is rf

    def test_read_uses_range_header(self):
        info = _make_info(file_size=10)
        rf = RemoteFile(info)
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"helloworld"
        mock_resp.status_code = 200
        with patch("httpx.Client") as mock_cls:
            mock_client = mock_cls.return_value
            mock_client.stream.return_value.__enter__.return_value = mock_resp
            data = rf.read()
        assert data == b"helloworld"
        mock_client.stream.assert_called_once_with(
            "GET", info.url, headers={"Range": "bytes=0-9"}
        )

    def test_read_partial_uses_range_header(self):
        info = _make_info(file_size=10)
        rf = RemoteFile(info)
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"hello"
        mock_resp.status_code = 206
        mock_resp.headers = {"Content-Range": "bytes 0-4/10"}
        with patch("httpx.Client") as mock_cls:
            mock_client = mock_cls.return_value
            mock_client.stream.return_value.__enter__.return_value = mock_resp
            data = rf.read(5)
        assert data == b"hello"
        mock_client.stream.assert_called_once_with(
            "GET", info.url, headers={"Range": "bytes=0-4"}
        )

    def test_read_stream_mode_uses_iter_bytes(self):
        info = _make_info(file_size=11)
        rf = RemoteFile(info)
        rf.set_stream(True)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_bytes.return_value = iter([b"hello", b" world"])
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_resp
        mock_ctx.__exit__.return_value = False
        with patch("httpx.Client") as mock_cls:
            mock_client = mock_cls.return_value
            mock_client.stream.return_value = mock_ctx
            data = rf.read()
        assert data == b"hello world"
        mock_client.stream.assert_called_once_with("GET", info.url)
        mock_resp.iter_bytes.assert_called_once_with(64 * 1024)

    def test_read_stream_mode_partial(self):
        info = _make_info(file_size=11)
        rf = RemoteFile(info)
        rf.set_stream(True)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_bytes.return_value = iter([b"hello", b" world"])
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_resp
        mock_ctx.__exit__.return_value = False
        with patch("httpx.Client") as mock_cls:
            mock_client = mock_cls.return_value
            mock_client.stream.return_value = mock_ctx
            data = rf.read(5)
        assert data == b"hello"
        mock_resp.iter_bytes.assert_called_once_with(64 * 1024)

    def test_close_cleans_up_stream_and_client(self):
        info = _make_info(file_size=5)
        rf = RemoteFile(info)
        rf.set_stream(True)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_bytes.return_value = iter([b"x"])
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_resp
        mock_ctx.__exit__.return_value = False
        with patch("httpx.Client") as mock_cls:
            mock_client = mock_cls.return_value
            mock_client.stream.return_value = mock_ctx
            rf.read(1)
            rf.close()
        mock_ctx.__exit__.assert_called_once_with(None, None, None)
        mock_client.close.assert_called_once()

    @pytest.mark.parametrize("stream", [False, True])
    def test_zero_read_does_not_request_or_advance(self, stream):
        rf = RemoteFile(_make_info(file_size=10))
        rf.set_stream(stream)
        with patch("httpx.Client") as client:
            assert rf.read(0) == b""
            assert rf.tell() == 0
            client.assert_not_called()

    def test_stream_varying_reads_seek_and_mode_changes(self):
        content = b"hello world"
        requests = []

        def respond(request):
            requests.append(request)
            range_header = request.headers.get("Range")
            if not range_header:
                return httpx.Response(200, content=content)
            start, end = range_header.removeprefix("bytes=").split("-")
            start, end = int(start), int(end) if end else len(content) - 1
            return httpx.Response(206, content=content[start:end + 1], headers={
                "Content-Range": f"bytes {start}-{end}/{len(content)}",
            })

        with RemoteFile(_make_info(file_size=len(content))) as rf:
            rf._client = httpx.Client(transport=httpx.MockTransport(respond))
            rf.set_stream(True)
            assert rf.read(2) == b"he"
            assert rf.read(4) == b"llo "
            assert rf.read() == b"world"
            rf.seek(1)
            assert rf.read(2) == b"el"
            rf.set_stream(False)
            assert rf.read(2) == b"lo"
            rf.set_stream(True)
            assert rf.read() == b" world"
            assert len(requests) == 4
            rf.seek(100)
            assert rf.tell() == 100 and rf.read() == b""
            with pytest.raises(ValueError, match="negative"):
                rf.seek(-1)
            assert rf.tell() == 100

    @pytest.mark.parametrize("stream", [False, True])
    def test_rejects_ignored_range_and_truncated_body(self, stream):
        with RemoteFile(_make_info(file_size=10)) as rf:
            rf._client = httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"short")))
            rf.set_stream(stream)
            with pytest.raises(OSError, match="end|length"):
                rf.read()
            rf.seek(2)
            with pytest.raises(OSError, match="byte range"):
                rf.read(2)

    def test_ignored_range_does_not_read_body(self):
        class UnreadBody(httpx.SyncByteStream):
            def __iter__(self):
                pytest.fail("response body must not be read")

        response = httpx.Response(200, stream=UnreadBody())
        with RemoteFile(_make_info(file_size=10)) as rf:
            rf._client = httpx.Client(transport=httpx.MockTransport(
                lambda request: response))
            rf.seek(2)
            with pytest.raises(OSError, match="byte range"):
                rf.read(2)
            assert rf.tell() == 2
        assert response.is_closed


class TestFileClientOpen:
    def test_open_returns_remote_file_with_correct_info(self):
        info = _make_info()
        mock_self = MagicMock()
        mock_self.url.return_value = info
        rf = FileClient.open(mock_self, "/some/path")
        assert isinstance(rf, RemoteFile)
        assert rf.name == info.file_name
        assert rf.size == info.file_size
        mock_self.url.assert_called_once_with("/some/path", user_agent=None)

    def test_open_with_file_object(self):
        info = _make_info(file_name="test.mkv", file_size=2048)
        mock_self = MagicMock()
        mock_self.url.return_value = info
        mock_file = MagicMock()
        rf = FileClient.open(mock_self, mock_file)
        assert isinstance(rf, RemoteFile)
        assert rf.name == "test.mkv"
        assert rf.size == 2048
        mock_self.url.assert_called_once_with(mock_file, user_agent=None)
