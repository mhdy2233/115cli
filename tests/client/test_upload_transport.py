import base64
import hashlib
from io import BytesIO

import httpcore
import httpx
import pytest

from cli115.client.general.base import APIClient
from cli115.client.general.upload import UploadClient


class _RecordingStream(httpcore.MockStream):

    def __init__(self, buffer, read_error=None):
        super().__init__(buffer)
        self.writes = []
        self.read_timeouts = []
        self.write_timeouts = []
        self.tls_timeouts = []
        self.read_error = read_error
        self.closed = False

    def read(self, max_bytes, timeout=None):
        self.read_timeouts.append(timeout)
        if not self._buffer and self.read_error is not None:
            raise self.read_error
        return super().read(max_bytes, timeout)

    def write(self, buffer, timeout=None):
        self.writes.append(buffer)
        self.write_timeouts.append(timeout)

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls_timeouts.append(timeout)
        return super().start_tls(ssl_context, server_hostname, timeout)

    def close(self):
        self.closed = True
        super().close()


class _RecordingBackend(httpcore.MockBackend):

    def __init__(self, buffer, read_error=None):
        super().__init__(buffer)
        self.stream = _RecordingStream(buffer, read_error)
        self.connect_timeouts = []

    def connect_tcp(self, host, port, timeout=None, **kwargs):
        self.connect_timeouts.append(timeout)
        return self.stream


@pytest.fixture
def upload_client():
    with APIClient(transport=httpx.MockTransport(lambda request: None)) as api:
        uploader = UploadClient(api)
        uploader._client.timeout = httpx.Timeout(
            connect=17.0, read=31.0, write=47.0, pool=59.0,
        )
        try:
            yield uploader
        finally:
            uploader._client.close()


def _assert_network_timeouts(backend):
    assert backend.connect_timeouts == [17.0]
    assert backend.stream.tls_timeouts == [17.0]
    assert set(backend.stream.read_timeouts) == {31.0}
    assert set(backend.stream.write_timeouts) == {47.0}


class TestUploadTransport:

    def test_part_sends_exact_length_and_releases_response(self, upload_client):
        content = b"file part\x00\xff\r\n" * 10_000
        md5 = base64.b64encode(hashlib.md5(content).digest())
        backend = _RecordingBackend([
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
            b'ETag: "part-etag"\r\nContent-MD5: ' + md5 + b"\r\n\r\nOK",
        ])
        token = {
            "AccessKeyId": "test-key",
            "AccessKeySecret": "test-secret",
            "SecurityToken": "test-token",
        }

        with httpcore.ConnectionPool(network_backend=backend) as pool:
            part = upload_client._oss_upload_part(
                "https://bucket.oss-cn-shenzhen.aliyuncs.com/file",
                "upload-session", 2, content, token, pool,
            )
            assert pool.connections[0].is_idle()

        headers, _, body = b"".join(backend.stream.writes).partition(b"\r\n\r\n")
        assert headers.startswith(b"PUT /file?partNumber=2&uploadId=upload-session ")
        assert b"content-length: " + str(len(content)).encode() in headers.lower()
        assert b"transfer-encoding" not in headers.lower()
        assert body == content
        assert max(map(len, backend.stream.writes[1:])) <= 64 * 1024
        assert part == {
            "PartNumber": 2,
            "ETag": '"part-etag"',
            "ContentMD5": md5.decode(),
        }
        _assert_network_timeouts(backend)

    def test_http_error_releases_response(self, upload_client):
        body = b"<Error><Code>AccessDenied</Code></Error>"
        backend = _RecordingBackend([
            b"HTTP/1.1 403 Forbidden\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\n\r\n" + body,
        ])
        token = {
            "AccessKeyId": "test-key",
            "AccessKeySecret": "test-secret",
            "SecurityToken": "test-token",
        }

        with httpcore.ConnectionPool(network_backend=backend) as pool:
            with pytest.raises(OSError, match="AccessDenied"):
                upload_client._oss_upload_part(
                    "https://bucket.oss-cn-shenzhen.aliyuncs.com/file",
                    "upload-session", 1, b"content", token, pool,
                )
            assert pool.connections[0].is_idle()

    def test_body_timeout_closes_connection(self, upload_client):
        backend = _RecordingBackend(
            [b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n"],
            read_error=httpcore.ReadTimeout("response stalled"),
        )
        token = {
            "AccessKeyId": "test-key",
            "AccessKeySecret": "test-secret",
            "SecurityToken": "test-token",
        }

        with httpcore.ConnectionPool(network_backend=backend) as pool:
            with pytest.raises(httpcore.ReadTimeout, match="response stalled"):
                upload_client._oss_upload_part(
                    "https://bucket.oss-cn-shenzhen.aliyuncs.com/file",
                    "upload-session", 1, b"content", token, pool,
                )
            assert backend.stream.closed
            assert not pool.connections

    def test_form_upload_has_timeouts_and_chunk_terminator(self, upload_client):
        backend = _RecordingBackend([
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}",
        ])
        request = upload_client._create_multipart_request(
            "https://bucket.oss-cn-shenzhen.aliyuncs.com/",
            data={"key": "file-key"}, filename="file.txt",
            file=BytesIO(b"file-content"),
        )

        with httpcore.ConnectionPool(network_backend=backend) as pool:
            response = pool.handle_request(request)
            try:
                assert response.read() == b"{}"
            finally:
                response.close()

        headers, _, body = b"".join(backend.stream.writes).partition(b"\r\n\r\n")
        assert b"transfer-encoding: chunked" in headers.lower()
        assert b"content-length" not in headers.lower()
        assert b'filename="file.txt"' in body
        assert b"file-content" in body
        assert body.endswith(b"0\r\n\r\n")
        _assert_network_timeouts(backend)
