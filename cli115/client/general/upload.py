from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.utils import formatdate
import hashlib
import hmac
import json
import logging
import time
from typing import BinaryIO
from urllib.parse import urlparse, urlsplit, urlunsplit
import uuid
import xml.etree.ElementTree as ET

import httpcore
import httpx
from p115cipher import ecdh_aes_decrypt, make_upload_payload

from cli115.exceptions import InstantUploadNotAvailableError
from .base import (
    APP_USER_AGENT,
    APP_VERSION,
    APIClient,
    DEFAULT_USER_AGENT,
    Endpoint,
)

# 16 MB default per chunk; also used as default threshold for switching to multipart upload
DEFAULT_PART_SIZE = 16 * 1024 * 1024
MULTIPART_UPLOAD_PART_SIZE = DEFAULT_PART_SIZE
_CHUNK_SIZE = 64 * 1024
logger = logging.getLogger("115cli.oss")


class _OSSTokenExpiredError(OSError):
    pass


def _raise_for_expired_oss_token(status: int, body: bytes) -> None:
    if status != 403:
        return
    try:
        code = ET.fromstring(body).findtext("Code")
    except ET.ParseError:
        return
    if code == "SecurityTokenExpired":
        raise _OSSTokenExpiredError(
            f"OSS security token expired: status={status}, body={body!r}"
        )


class UploadClient:
    """Handles all upload-related operations.

    Provides two public upload methods:
      - ``simple_upload``: single-request upload via the sample-upload endpoint.
      - ``multipart_upload``: OSS multipart upload for large files.
    """

    def __init__(self, api: APIClient):
        self._api = api
        self._client = httpx.Client(
            timeout=httpx.Timeout(120.0, connect=30.0),
            headers={
                "User-Agent": api.headers.get("User-Agent", DEFAULT_USER_AGENT),
                "Referer": "https://115.com/",
            }
        )

    def instant_upload(
        self,
        *,
        file: BinaryIO,
        filename: str,
        file_size: int,
        sha1: str,
        dir_id: str,
    ) -> None:
        """Attempt an instant upload (zero-copy if the file is already on 115).

        Raises:
            InstantUploadNotAvailableError: If the server does not have the file,
                with ``response_data`` carrying the initupload response so the
                caller can reuse bucket/object/callback for a multipart upload.
        """

        def read_range(range_str: str) -> bytes:
            start, end = [int(x) for x in range_str.split("-")]
            file.seek(start)
            return file.read(end - start + 1)

        upload_info = self._api.get(Endpoint.PROAPI + "/app/uploadinfo").json()

        payload = {
            "filename": filename,
            "fileid": sha1.upper(),
            "filesize": file_size,
            "target": f"U_1_{dir_id}",
            "appid": 0,
            "sign_key": "",
            "sign_val": "",
            "topupload": "true",
            "appversion": APP_VERSION,
            "userid": upload_info["user_id"],
            "userkey": upload_info["userkey"],
        }
        logger.debug(
            f"POST /initupload.php payload: fileid={payload['fileid']}, size={payload['filesize']}, target={payload['target']}"
        )
        resp = self._post_upload_init(payload)
        status = int(resp["status"])
        logger.debug(f"initupload response status: {status}")
        if status == 7:
            sign_check = str(resp.get("sign_check", ""))
            payload["sign_key"] = str(resp.get("sign_key", ""))
            payload["sign_val"] = (
                hashlib.sha1(read_range(sign_check)).hexdigest().upper()
            )
            logger.debug(
                f"Server requested sign_check range {sign_check}, computed sign_val: {payload['sign_val']}"
            )
            resp = self._post_upload_init(payload)
            status = int(resp["status"])
            logger.debug(f"initupload second response status: {status}")

        if status != 2:
            logger.debug(f"Instant upload failed: status={status}")
            raise InstantUploadNotAvailableError(
                "instant upload is not available (file not found on server)",
                response_data=resp,
            )

    def simple_upload(self, file: BinaryIO, *, pid: str, filename: str) -> dict:
        """Upload a file using the simple (sample) upload endpoint.

        Args:
            file: Readable binary file object (seeked to start).
            pid: Parent directory ID on 115.
            filename: Destination filename.

        Returns:
            Parsed JSON response from the upload callback.
        """
        sample_info = self._api.post(
            Endpoint.UPLB + "/3.0/sampleinitupload.php",
            data={"filename": filename, "target": f"U_1_{pid}"},
        ).json()

        fields = {
            "name": filename,
            "key": sample_info["object"],
            "policy": sample_info["policy"],
            "OSSAccessKeyId": sample_info["accessid"],
            "success_action_status": "200",
            "callback": sample_info["callback"],
            "signature": sample_info["signature"],
        }

        request = self._create_multipart_request(
            sample_info["host"],
            data=fields,
            filename=filename,
            file=file,
        )

        with httpcore.ConnectionPool(retries=2) as pool:
            response = pool.handle_request(request)
            try:
                body = response.read()
            finally:
                response.close()

        if response.status >= 400:
            raise OSError(
                f"sample upload request failed: status={response.status}, body={body!r}"
            )

        data = json.loads(body)
        data["oss_info"] = sample_info
        return data

    def multipart_upload(
        self,
        file: BinaryIO,
        *,
        bucket: str,
        object: str,
        callback: dict,
        part_size: int = DEFAULT_PART_SIZE,
    ) -> dict:
        """Upload a file using the OSS multipart protocol.

        Args:
            file: Readable binary file object (seeked to start).
            bucket: OSS bucket name.
            object: OSS object key.
            callback: Callback payload from the initupload response.
            part_size: Size in bytes of each uploaded part (defaults to 16 MB).
                One part is buffered per upload to allow retries.

        Returns:
            Parsed JSON response from the OSS complete-upload callback.
        """
        if part_size <= 0:
            raise ValueError("part_size must be positive")

        url = f"https://{bucket}.oss-cn-shenzhen.aliyuncs.com/{object}"
        token: dict | None = None
        token_refresh_at = 0.0

        def oss_request(operation):
            nonlocal token, token_refresh_at
            for attempt in range(3):
                if token is None or time.monotonic() >= token_refresh_at:
                    logger.debug("fetching OSS STS token for multipart upload")
                    token = self._get_oss_token()
                    token_refresh_at = time.monotonic() + 900
                    if expiration := token.get("Expiration"):
                        try:
                            expires_at = datetime.fromisoformat(str(expiration))
                        except ValueError:
                            pass
                        else:
                            if expires_at.tzinfo is None:
                                expires_at = expires_at.replace(tzinfo=timezone.utc)
                            token_refresh_at = min(
                                token_refresh_at,
                                time.monotonic() + expires_at.timestamp()
                                - time.time() - 60,
                            )
                try:
                    return operation(token)
                except _OSSTokenExpiredError:
                    if attempt == 2:
                        raise
                    logger.debug("OSS token expired; refreshing and retrying request")
                    token = None
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    if attempt == 2:
                        raise

        upload_id = oss_request(
            lambda token: self._oss_multipart_upload_init(url, token)
        )
        logger.debug(f"Initialized OSS multipart session: upload_id={upload_id}, url={url}")

        parts: list[dict] = []
        part_number = 1
        with httpcore.ConnectionPool(retries=2) as pool:
            while True:
                # Buffer one part so its length is known and retries replay it exactly.
                chunk = file.read(part_size)
                if not chunk:
                    break
                if len(chunk) < part_size:
                    buffer = bytearray(chunk)
                    while len(buffer) < part_size:
                        more = file.read(part_size - len(buffer))
                        if not more:
                            break
                        buffer.extend(more)
                    chunk = bytes(buffer)
                    del buffer

                logger.debug(f"uploading part {part_number} ({len(chunk)} bytes)...")
                part = oss_request(
                    lambda token: self._oss_upload_part(
                        url, upload_id, part_number, chunk, token, pool
                    )
                )
                part["Size"] = len(chunk)
                local_md5 = base64.b64encode(hashlib.md5(chunk).digest()).decode()
                server_md5 = part.pop("ContentMD5", "")
                logger.debug(f"Part {part_number} uploaded: ETag={part.get('ETag')}, MD5={server_md5}")
                if server_md5 and server_md5 != local_md5:
                    raise RuntimeError(
                        f"part {part_number} MD5 mismatch: "
                        f"expected {local_md5!r}, server returned {server_md5!r}"
                    )

                parts.append(part)
                part_number += 1
                del chunk

        logger.debug(f"Completing OSS multipart upload ({len(parts)} parts)...")
        return oss_request(
            lambda token: self._oss_multipart_upload_complete(
                url, upload_id, callback, parts, token
            )
        )

    def _post_upload_init(self, payload: dict) -> dict:
        resp = self._api.post(
            Endpoint.UPLB + "/4.0/initupload.php",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": APP_USER_AGENT,
            },
            **make_upload_payload(payload.copy()),
        )
        return json.loads(ecdh_aes_decrypt(resp.content))

    def _get_oss_token(self) -> dict:
        """Fetch a short-lived STS token from 115 for signing OSS requests."""
        return self._api.get(Endpoint.UPLB + "/3.0/gettoken.php").json()

    def _oss_multipart_upload_init(self, url: str, token: dict) -> str:
        """Initiate an OSS multipart upload session and return the upload_id."""
        init_url = f"{url}?sequential=1&uploads=1"
        headers = self._oss_sign(init_url, "POST", token)
        resp = self._client.post(init_url, headers=headers)
        _raise_for_expired_oss_token(resp.status_code, resp.content)
        resp.raise_for_status()
        upload_id = ET.fromstring(resp.content).findtext("UploadId")
        if not upload_id:
            raise RuntimeError(f"UploadId not found in OSS response: {resp.content!r}")
        return upload_id

    def _oss_upload_part(
        self,
        url: str,
        upload_id: str,
        part_number: int,
        content: bytes,
        token: dict,
        pool: httpcore.ConnectionPool,
    ) -> dict:
        """Upload one part and return its part metadata dict."""
        part_url = f"{url}?partNumber={part_number}&uploadId={upload_id}"
        headers = self._oss_sign(part_url, "PUT", token)
        headers["content-length"] = str(len(content))
        # Keep individual socket writes bounded even when a part is large.
        stream = (
            content[offset:offset + _CHUNK_SIZE]
            for offset in range(0, len(content), _CHUNK_SIZE)
        )
        request = httpcore.Request(
            "PUT", part_url, headers=headers, content=stream,
            extensions={"timeout": self._client.timeout.as_dict()},
        )
        response = pool.handle_request(request)
        try:
            body = response.read()
        finally:
            response.close()
        if response.status >= 400:
            _raise_for_expired_oss_token(response.status, body)
            raise OSError(
                f"part {part_number} upload failed: "
                f"status={response.status}, body={body!r}"
            )
        headers_lower = {k.lower(): v for k, v in response.headers}
        etag = headers_lower.get(b"etag", b"").decode()
        server_md5 = headers_lower.get(b"content-md5", b"").decode()
        return {"PartNumber": part_number, "ETag": etag, "ContentMD5": server_md5}

    def _oss_multipart_upload_complete(
        self,
        url: str,
        upload_id: str,
        callback: dict,
        parts: list[dict],
        token: dict,
    ) -> dict:
        """Finalize the multipart upload and trigger the 115 server callback."""
        complete_url = f"{url}?uploadId={upload_id}"
        xml_body = (
            b"<CompleteMultipartUpload>"
            + b"".join(
                (
                    f"<Part><PartNumber>{p['PartNumber']}</PartNumber>"
                    f"<ETag>{p['ETag']}</ETag></Part>"
                ).encode()
                for p in parts
            )
            + b"</CompleteMultipartUpload>"
        )
        extra_headers = {
            "x-oss-callback": base64.b64encode(callback["callback"].encode()).decode(),
            "x-oss-callback-var": base64.b64encode(
                callback["callback_var"].encode()
            ).decode(),
            "content-type": "text/xml",
        }
        headers = self._oss_sign(complete_url, "POST", token, extra_headers)
        resp = self._client.post(complete_url, headers=headers, content=xml_body)
        _raise_for_expired_oss_token(resp.status_code, resp.content)
        resp.raise_for_status()
        return json.loads(resp.content)

    def _oss_sign(
        self,
        url: str,
        method: str,
        token: dict,
        extra_headers: dict | None = None,
    ) -> dict:
        """Build an OSS v1 (HMAC-SHA1) signed headers dict for the given request."""
        headers: dict[str, str] = {}
        if extra_headers:
            headers.update(extra_headers)
        headers["x-oss-security-token"] = token["SecurityToken"]
        headers.setdefault("content-md5", "")
        headers.setdefault("content-type", "")
        date = headers["date"] = formatdate(usegmt=True)

        urlp = urlsplit(url)
        bucket = urlp.hostname.partition(".")[0]
        headers["host"] = urlp.netloc
        oss_header_lines = "\n".join(
            f"{k}:{v}" for k, v in sorted(headers.items()) if k.startswith("x-oss-")
        )
        canonical_resource = (
            f"/{bucket}{urlunsplit(urlp._replace(scheme='', netloc=''))}"
        )
        string_to_sign = (
            f"{method.upper()}\n"
            f"{headers['content-md5']}\n"
            f"{headers['content-type']}\n"
            f"{date}\n"
            f"{oss_header_lines}\n"
            f"{canonical_resource}"
        )
        signature = base64.b64encode(
            hmac.digest(
                token["AccessKeySecret"].encode(),
                string_to_sign.encode(),
                "sha1",
            )
        ).decode()
        headers["authorization"] = f"OSS {token['AccessKeyId']}:{signature}"
        return headers

    def _create_multipart_request(
        self,
        url: str,
        *,
        data: dict[str, str],
        filename: str,
        file: BinaryIO,
    ) -> httpcore.Request:
        parsed = urlparse(url)
        boundary = uuid.uuid4().hex
        content = self._iter_multipart_content(
            boundary=boundary,
            data=data,
            filename=filename,
            file=file,
        )
        return httpcore.Request(
            method="POST",
            url=url,
            headers={
                "Host": parsed.netloc,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Transfer-Encoding": "chunked",
            },
            content=content,
            extensions={"timeout": self._client.timeout.as_dict()},
        )

    def _iter_multipart_content(
        self,
        *,
        boundary: str,
        data: dict[str, str],
        filename: str,
        file: BinaryIO,
    ):
        boundary_bytes = boundary.encode("ascii")

        for key, value in data.items():
            yield b"--" + boundary_bytes + b"\r\n"
            yield (f'Content-Disposition: form-data; name="{key}"\r\n\r\n').encode(
                "utf-8"
            )
            yield str(value).encode("utf-8")
            yield b"\r\n"

        yield b"--" + boundary_bytes + b"\r\n"
        yield (
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        ).encode("utf-8")
        yield b"Content-Type: application/octet-stream\r\n\r\n"

        while chunk := file.read(_CHUNK_SIZE):
            yield chunk

        yield b"\r\n"
        yield b"--" + boundary_bytes + b"--\r\n"
