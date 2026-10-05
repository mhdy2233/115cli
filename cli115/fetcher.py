from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from os import PathLike
from pathlib import PureWindowsPath
import tempfile
from typing import Sequence

from blinker import Signal
from pathspec import PathSpec

from cli115.client import Client
from cli115.client.base import MAX_PAGE_SIZE
from cli115.client.models import Directory, File, Progress
from cli115.helpers import sha1_file

DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024


def local_filename(name: str) -> str:
    """Reject remote names that cannot safely be used as one local component."""
    is_reserved = getattr(
        os.path, "isreserved", lambda name: PureWindowsPath(name).is_reserved()
    )
    if (
        not name
        or name in {".", ".."}
        or any(char in name for char in '/\\\x00')
        or (
            os.name == "nt"
            and (
                any(char in name for char in ':*?"<>|')
                or name.endswith((".", " "))
                or is_reserved(name)
            )
        )
    ):
        raise ValueError(f"invalid local filename: {name!r}")
    return name


class DownloadStatus:
    def __init__(self) -> None:
        self._is_completed: bool = False
        self.is_skipped: bool = False
        self.on_message: Signal = Signal()
        self.on_download: Signal = Signal()
        self.on_integrity_check: Signal = Signal()
        self.on_complete: Signal = Signal()

    @property
    def is_completed(self) -> bool:
        return self._is_completed

    def set_message(self, message: str) -> None:
        self.on_message.send(self, message=message)

    @contextmanager
    def start_download(self, file_size: int):
        progress = Progress(file_size)
        self.on_download.send(self, progress=progress)
        self.set_message("downloading...")
        yield progress

    @contextmanager
    def start_integrity_check(self, file_size: int):
        progress = Progress(file_size)
        self.on_integrity_check.send(self, progress=progress)
        self.set_message("checking file integrity...")
        yield progress

    def complete(
        self, *, skipped: bool = False, skip_reason: str = "size and sha1 match"
    ) -> None:
        if not self._is_completed:
            self._is_completed = True
            self.is_skipped = skipped
            self.on_complete.send(self)
            self.set_message(
                f"already exists ({skip_reason}), skipped"
                if skipped else "download completed"
            )


class FetchEntry:
    """A single file to be downloaded."""

    def __init__(
        self,
        remote_entry: File,
        local_path: str | PathLike[str],
    ):
        self.remote_entry = remote_entry
        self.local_path = local_path
        self.status = DownloadStatus()
        self.error: Exception | None = None


class Fetcher:
    """Manages downloading files and directories from remote filesystem."""

    def __init__(
        self,
        client: Client,
        *,
        dry_run: bool = False,
        user_agent: str | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_workers: int = 1,
        dedup_by_name: bool = False,
    ):
        if chunk_size <= 0:
            raise ValueError("chunk size must be greater than zero")
        if max_workers < 1:
            raise ValueError("max workers must be greater than zero")
        self._client = client
        self.dry_run = dry_run
        self.user_agent = user_agent
        self.chunk_size = chunk_size
        self.max_workers = max_workers
        self.dedup_by_name = dedup_by_name
        self.entries: list[FetchEntry] = []
        self.on_entry_added = Signal()

    def fetch(
        self,
        remote_entry: Directory | File,
        local_path: str | os.PathLike[str],
        *,
        check_integrity: bool = False,
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
    ) -> str | None:
        """Fetch a remote file or directory to a local destination path.

        Existing files are skipped when their size and SHA-1 match, or without
        reading their contents when ``dedup_by_name`` is enabled.
        Directory downloads use at most ``max_workers`` concurrent workers.

        Args:
            remote_entry: Remote file or directory entry to fetch.
            local_path: Local destination path. For files, this is the output
                file path. For directories, this is the destination directory.
            check_integrity: When ``True``, verify downloaded files by size and
                SHA-1.
            include: Optional glob patterns used to include files when fetching
                a directory.
            exclude: Optional glob patterns used to exclude files when fetching
                a directory.

        Returns:
            The resolved local destination path, or ``None`` in dry-run mode.

        Raises:
            FileExistsError: If fetching a directory to an existing local file
                path.
            ValueError: If integrity checks fail for a downloaded file.
        """

        local_path = os.path.abspath(local_path)
        if remote_entry.is_directory:
            self._fetch_directory(
                remote_entry,
                local_path,
                check_integrity=check_integrity,
                include=include,
                exclude=exclude,
            )
        else:
            self._fetch_file(
                remote_entry,
                local_path,
                check_integrity=check_integrity,
            )
        if not self.dry_run:
            return local_path

    def _fetch_file(
        self,
        remote_entry: File,
        local_path: str | os.PathLike[str],
        *,
        check_integrity: bool = False,
    ) -> str | None:
        entry = FetchEntry(remote_entry, local_path)
        self.entries.append(entry)
        self.on_entry_added.send(self, entries=[entry])

        try:
            self._download_entry(
                remote_entry,
                local_path,
                check_integrity=check_integrity,
                status=entry.status,
            )
        except Exception as exc:
            entry.error = exc
            raise

    def _fetch_directory(
        self,
        remote_entry: Directory,
        dest_path: str | os.PathLike[str],
        *,
        check_integrity: bool = False,
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
    ) -> str | None:
        if os.path.isfile(dest_path):
            raise FileExistsError(f"cannot fetch directory to a file path: {dest_path}")

        include_spec = PathSpec.from_lines("gitignore", include) if include else None
        exclude_spec = PathSpec.from_lines("gitignore", exclude) if exclude else None

        files = self._collect_files(
            remote_entry,
            include=include_spec,
            exclude=exclude_spec,
        )
        entries = [
            FetchEntry(
                remote_file,
                os.path.join(dest_path, rel_path),
            )
            for remote_file, rel_path in files
        ]
        self.entries.extend(entries)
        self.on_entry_added.send(self, entries=entries)

        def download_entry(entry: FetchEntry) -> None:
            try:
                self._download_entry(
                    entry.remote_entry,
                    entry.local_path,
                    check_integrity=check_integrity,
                    status=entry.status,
                )
            except Exception as exc:
                entry.error = exc

        if self.max_workers > 1 and len(entries) > 1:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                list(executor.map(download_entry, entries))
        else:
            for entry in entries:
                download_entry(entry)

    def _download_entry(
        self,
        entry: File,
        local_path: str | os.PathLike[str],
        *,
        check_integrity: bool,
        status: DownloadStatus,
    ) -> None:
        if os.path.isdir(local_path):
            raise IsADirectoryError(f"cannot fetch file to a directory path: {local_path}")
        if self.dedup_by_name and os.path.isfile(local_path):
            status.complete(skipped=True, skip_reason="filename match")
            return
        if self.dry_run:
            return

        if os.path.isfile(local_path) and os.path.getsize(local_path) == entry.size:
            with (
                open(local_path, "rb") as local_file,
                status.start_integrity_check(entry.size) as progress,
                progress.patch_file(local_file),
            ):
                sha1, size = sha1_file(local_file)
            if size == entry.size and sha1 == entry.sha1.upper():
                status.complete(skipped=True)
                return

        parent = os.path.dirname(local_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w+b", dir=parent or ".", prefix=".115cli-", suffix=".part",
                delete=False,
            ) as local_file:
                temporary_path = local_file.name
                with (
                    self._client.file.open(entry, user_agent=self.user_agent) as remote,
                    status.start_download(entry.size) as progress,
                ):
                    remote.set_stream(True)
                    while True:
                        chunk = remote.read(self.chunk_size)
                        if not chunk:
                            break
                        local_file.write(chunk)
                        progress.update(len(chunk))

                size = local_file.tell()
                if size != entry.size:
                    raise ValueError(
                        f"size mismatch: expected {entry.size}, got {size}"
                    )
                if check_integrity:
                    with (
                        status.start_integrity_check(entry.size) as progress,
                        progress.patch_file(local_file),
                    ):
                        sha1, size = sha1_file(local_file)
                    if sha1 != entry.sha1.upper():
                        raise ValueError(
                            f"sha1 mismatch: expected {entry.sha1}, got {sha1}"
                        )
                    status.set_message("file integrity verified")
            os.replace(temporary_path, local_path)
            temporary_path = None
            status.complete()
        finally:
            if temporary_path is not None:
                os.unlink(temporary_path)

    def _collect_files(
        self,
        remote_entry: Directory,
        *,
        include: PathSpec | None,
        exclude: PathSpec | None,
    ) -> list[tuple[File, str]]:
        rv: list[tuple[File, str]] = []
        targets: dict[str, Directory | File] = {}

        def walk(current: Directory, rel_root: str) -> None:
            for child in self._client.file.list(current, page_size=MAX_PAGE_SIZE):
                rel_path = os.path.join(rel_root, local_filename(child.name))
                if not child.is_directory:
                    if include is not None and not include.match_file(rel_path):
                        continue
                    if exclude is not None and exclude.match_file(rel_path):
                        continue

                target = os.path.normcase(rel_path)
                previous = targets.get(target)
                if previous is not None:
                    if (
                        previous.id == child.id
                        and previous.is_directory == child.is_directory
                    ):
                        continue
                    raise FileExistsError(
                        f"multiple remote entries map to local path: {rel_path}"
                    )
                targets[target] = child
                if child.is_directory:
                    walk(child, rel_path)
                    continue

                rv.append((child, rel_path))

        walk(remote_entry, "")
        return rv
