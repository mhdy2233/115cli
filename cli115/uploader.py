from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import os
from os import PathLike
from typing import Sequence

from blinker import Signal
from pathspec import PathSpec

from cli115.client import Client
from cli115.client.models import Directory, File, UploadStatus
from cli115.helpers import format_size, join_path, normalize_path

logger = logging.getLogger("115cli.uploader")
class UploadEntry:
    """A single file to be uploaded."""

    def __init__(
        self,
        local_path: str | PathLike[str],
        remote_path: str | PathLike[str],
    ):
        self.local_path = local_path
        self.remote_path = remote_path
        self.size = os.path.getsize(local_path) if os.path.isfile(local_path) else 0
        self.status = UploadStatus()
        self.error: Exception | None = None


class Uploader:
    """Manages uploading files and directories to the remote filesystem.

    The constructor takes the authenticated client. Call :meth:`upload`
    to queue and upload a local file or directory to a remote path.

    Attributes:
        entries: List of :class:`UploadEntry` objects queued for upload.
        on_entry_added: Signal emitted when new entries are added.
            Receivers get ``(sender, entries=<list of new entries>)``.
    """

    def __init__(
        self,
        client: Client,
        *,
        dry_run: bool = False,
        part_size: int | None = None,
        max_workers: int = 1,
    ):
        self._client = client
        self.dry_run = dry_run
        self.part_size = part_size
        self.max_workers = max_workers if max_workers and max_workers > 0 else 1
        self.entries: list[UploadEntry] = []
        self.on_entry_added = Signal()

    def upload(
        self,
        local_path: str | os.PathLike[str],
        remote_path: str,
        *,
        instant_only: int | None = None,
        part_size: int | None = None,
        max_workers: int | None = None,
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
        no_target_dir: bool = False,
    ) -> Directory | File | None:
        """Upload a local file or directory to the remote filesystem.

        If ``local_path`` is a directory, the directory tree is uploaded
        recursively into ``remote_path``. If ``local_path`` is a file and
        ``remote_path`` points to an existing remote directory, the local
        filename is appended to the destination path. Uses ``client.file.upload``
        and ``client.file.create_directory`` under the hood.

        When uploading a directory, ``include`` and ``exclude`` glob patterns
        control which files are transferred.  Patterns follow the gitignore /
        VS Code glob syntax — for example ``"**/*.log"`` excludes all log files
        and ``"temp/**"`` excludes the ``temp/`` subtree.  See
        https://code.visualstudio.com/docs/editor/glob-patterns for the full
        syntax reference.  Patterns are matched against paths relative to the
        root of the uploaded directory (using ``/`` separators).

        Args:
            local_path: Path to the local file or directory.
            remote_path: Destination path on the remote.
            instant_only: If set to a byte threshold (e.g. ``100 * 1024 * 1024``
                for 100 MB), files at or above that size will be forced to use
                instant upload only.  Values below
                :data:`~cli115.client.base.MIN_INSTANT_UPLOAD_SIZE` (2 MB) are
                ignored.  Raises
                :class:`~cli115.exceptions.InstantUploadNotAvailableError` when
                instant upload is unavailable for a qualifying file.
            part_size: Part size in bytes for multipart uploads. Defaults to
                the uploader's ``part_size`` or 16 MB.
            max_workers: Number of concurrent file uploads when uploading a
                directory. Defaults to the uploader's ``max_workers`` (1).
            include: Glob patterns for files to include.  Only files matching at
                least one pattern are uploaded.  ``None`` means include all files.
            exclude: Glob patterns for files to exclude.  Files matching any
                pattern are skipped.  ``None`` means exclude nothing.

        Returns:
            The created or existing remote directory entry when uploading a directory, or the
            result returned by ``client.file.upload`` when uploading a file. `None`
            is returned when ``dry_run`` is ``True``.

        Raises:
            FileExistsError: If attempting to upload a directory to a remote file path.
            FileNotFoundError: If the target remote directory does not exist.
        """

        local_path = os.path.abspath(local_path)
        effective_part_size = part_size if part_size is not None else self.part_size
        effective_max_workers = (
            max_workers if max_workers is not None else self.max_workers
        )
        if effective_max_workers < 1:
            effective_max_workers = 1

        if os.path.isdir(local_path):
            return self._upload_directory(
                local_path,
                remote_path,
                instant_only=instant_only,
                part_size=effective_part_size,
                max_workers=effective_max_workers,
                include=include,
                exclude=exclude,
                no_target_dir=no_target_dir,
            )
        else:
            return self._upload_file(
                local_path,
                remote_path,
                instant_only=instant_only,
                part_size=effective_part_size,
            )

    def _upload_file(
        self,
        local_path: str,
        remote_path: str,
        *,
        instant_only: int | None,
        part_size: int | None = None,
    ) -> File | None:
        # If remote path points to an existing directory, append filename
        try:
            entry = self._client.file.stat(remote_path)
            if entry.is_directory:
                file_name = os.path.basename(local_path)
                remote_path = join_path(remote_path, file_name)
        except FileNotFoundError:
            pass

        upload_entry = UploadEntry(local_path, remote_path)
        self.entries.append(upload_entry)
        self.on_entry_added.send(self, entries=[upload_entry])

        if not self.dry_run:
            upload_entry.status.start()
            try:
                return self._client.file.upload(
                    remote_path,
                    local_path,
                    instant_only=instant_only,
                    part_size=part_size,
                    status=upload_entry.status,
                )
            finally:
                upload_entry.status._complete()

    def _upload_directory(
        self,
        local_path: str,
        dest_path: str,
        *,
        no_target_dir: bool = False,
        instant_only: int | None = None,
        part_size: int | None = None,
        max_workers: int = 1,
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
    ) -> Directory | None:
        dest_id: str | None = None
        try:
            dest_id = self._client.file._resolve_dir_id(dest_path)
            if not no_target_dir:
                dir_name = os.path.basename(local_path)
                dest_path = join_path(dest_path, dir_name)
                try:
                    dest_id = self._client.file._resolve_dir_id(dest_path)
                except FileNotFoundError:
                    dest_id = None
        except FileNotFoundError:
            dest_id = None
        include_spec = PathSpec.from_lines("gitignore", include) if include else None
        exclude_spec = PathSpec.from_lines("gitignore", exclude) if exclude else None

        # Collect files to upload
        files = _collect_files(
            local_path,
            dest_path,
            include=include_spec,
            exclude=exclude_spec,
        )
        logger.debug(
            f"Scanned local directory '{local_path}': found {len(files)} files"
        )

        # 1. Fetch remote directory tree to get all existing files
        existing_dirs: dict[str, Directory] = {}
        existing_files: set[str] = set()

        if dest_id is not None:
            logger.debug(
                f"Fetching remote directory tree for destination '{dest_path}' (id={dest_id})..."
            )
            existing_dirs, existing_files = fetch_remote_tree(
                self._client, dest_path, root_dir_id=dest_id
            )
            logger.debug(
                f"Remote tree fetched: {len(existing_files)} existing files found on cloud"
            )

        # 2. Perform in-memory comparison: ONLY keep files that do NOT exist on cloud
        needed_files: list[tuple[str, str]] = []
        for lf, rf in files:
            norm_rf = normalize_path(rf)
            if norm_rf in existing_files:
                # File already exists on cloud, skip
                continue
            needed_files.append((lf, rf))

        logger.info(
            f"Comparison completed: {len(files) - len(needed_files)} files already exist on cloud, {len(needed_files)} files queued for upload"
        )

        # 3. Only queue files that actually need to be uploaded
        entries = [UploadEntry(lf, rf) for lf, rf in needed_files]
        self.entries.extend(entries)
        self.on_entry_added.send(self, entries=entries)

        norm_dest_path = normalize_path(dest_path)
        if self.dry_run or not entries:
            logger.debug("Dry run or no files to upload. Returning early.")
            if dest_id is not None:
                return existing_dirs.get(norm_dest_path) or Directory(
                    id=dest_id,
                    parent_id="",
                    path=dest_path,
                    name=os.path.basename(dest_path),
                    pickcode="",
                    created_time=None,
                    modified_time=None,
                    open_time=None,
                )
            return None

        # 4. ONLY create/resolve parent directories for the needed files (grouped by directory)
        needed_dirs = _collect_dirs(needed_files, dest_path)
        dir_id_map: dict[str, str] = {
            d: entry.id for d, entry in existing_dirs.items() if entry.id
        }

        if dest_id is not None:
            dir_id_map[norm_dest_path] = dest_id
            dir_id_map[dest_path] = dest_id
            dest_dir = existing_dirs.get(norm_dest_path) or Directory(
                id=dest_id,
                parent_id="",
                path=dest_path,
                name=os.path.basename(dest_path),
                pickcode="",
                created_time=None,
                modified_time=None,
                open_time=None,
            )
        else:
            logger.debug(f"Creating root destination directory: '{dest_path}'")
            dest_dir = self._client.file.create_directory(dest_path, parents=True)
            dir_id_map[norm_dest_path] = dest_dir.id
            dir_id_map[dest_path] = dest_dir.id

        for d in sorted(needed_dirs):
            norm_d = normalize_path(d)
            if norm_d not in dir_id_map and d not in dir_id_map:
                logger.debug(f"Creating remote subdirectory: '{d}'")
                sub_dir = self._client.file.create_directory(d, parents=True)
                dir_id_map[norm_d] = sub_dir.id
                dir_id_map[d] = sub_dir.id
        # 5. Upload files
        logger.info(
            f"Starting upload of {len(entries)} file(s) with max_workers={max_workers}..."
        )

        def _upload_single_entry(upload_entry: UploadEntry) -> None:
            upload_entry.status.start()
            parent_d = upload_entry.remote_path.rsplit("/", 1)[0]
            logger.debug(
                f"[{upload_entry.local_path} -> {upload_entry.remote_path}] Starting upload ({format_size(upload_entry.size)})"
            )
            try:
                self._client.file.upload(
                    upload_entry.remote_path,
                    upload_entry.local_path,
                    instant_only=instant_only,
                    part_size=part_size,
                    check_exists=False,
                    dir_id=dir_id_map.get(parent_d),
                    status=upload_entry.status,
                )
                logger.debug(
                    f"[{upload_entry.remote_path}] Upload successfully finished"
                )
            except Exception as exc:
                upload_entry.error = exc
                logger.error(
                    f"[{upload_entry.remote_path}] Upload failed: {exc}", exc_info=True
                )
            finally:
                upload_entry.status._complete()

        if max_workers > 1 and len(entries) > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                list(executor.map(_upload_single_entry, entries))
        else:
            for upload_entry in entries:
                _upload_single_entry(upload_entry)

        return dest_dir
def _collect_files(
    local_dir: str,
    remote_dir: str,
    *,
    include: PathSpec | None = None,
    exclude: PathSpec | None = None,
) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for root, dirs, fnames in os.walk(local_dir):
        dirs.sort()
        rel_root = os.path.relpath(root, local_dir).replace("\\", "/")
        if rel_root == ".":
            rel_root = ""
        for fname in sorted(fnames):
            rel_path = f"{rel_root}/{fname}" if rel_root else fname
            if include is not None and not include.match_file(rel_path):
                continue
            if exclude is not None and exclude.match_file(rel_path):
                continue
            local_file = os.path.join(root, fname)
            remote_file = join_path(remote_dir, rel_path)
            result.append((local_file, remote_file))
    return result


def _collect_dirs(files: list[tuple[str, str]], dest_path: str) -> set[str]:
    dirs: set[str] = set()
    for _local, remote in files:
        parent = remote.rsplit("/", 1)[0]
        while parent and parent != dest_path:
            dirs.add(parent)
            parent = parent.rsplit("/", 1)[0]
    return dirs


def parse_115_export_tree(
    content: str, root_dest_path: str
) -> tuple[set[str], set[str]]:
    """Parse 115 exported directory tree text into normalized directory paths and file paths.

    Supports both 115 official directory tree format (| | |-) and standard ASCII tree format.

    Returns:
        tuple of (existing_dir_paths, existing_file_paths)
    """
    root_dest_path = normalize_path(root_dest_path)
    lines = content.splitlines()
    if not lines:
        return {root_dest_path}, set()

    parsed_lines: list[tuple[int, str]] = []
    # Check if content is 115 official format (contains '|-' or '|——')
    is_115_official = any(("|-" in l or "|——" in l) for l in lines[:30])

    if is_115_official:
        for line in lines:
            raw = line.rstrip("\r\n")
            if not raw.strip() or "|" not in raw:
                continue
            bar_count = raw.count("|")
            if "|——" in raw:
                name = raw.rsplit("|——", 1)[-1].strip()
            elif "|-" in raw:
                name = raw.rsplit("|-", 1)[-1].strip()
            else:
                name = raw.lstrip("| -").strip()
            if not name:
                continue
            depth = bar_count - 1
            parsed_lines.append((depth, name))
    else:
        for line_idx, line in enumerate(lines):
            raw = line.rstrip("\r\n")
            if not raw.strip():
                continue
            clean = raw
            indent = 0
            while clean and (
                clean[0] in (" ", "\t", "│", "|", "├", "└", "─", "—", "-")
            ):
                if clean[0] == "\t":
                    indent += 4
                else:
                    indent += 1
                clean = clean[1:]
            name = clean.strip()
            if not name or (indent == 0 and line_idx == 0):
                continue
            if "directories" in name and "files" in name:
                continue
            if "(" in name and name.endswith(")"):
                name = name.rsplit("(", 1)[0].strip()
            depth = (indent // 4) if indent >= 4 else (indent // 2 if indent > 0 else 1)
            parsed_lines.append((depth, name))

    if not parsed_lines:
        return {root_dest_path}, set()

    stack = [root_dest_path]
    existing_dirs = {root_dest_path}
    existing_files: set[str] = set()

    total = len(parsed_lines)
    for i in range(total):
        depth, name = parsed_lines[i]
        if depth == 0:
            continue

        is_dir = i + 1 < total and parsed_lines[i + 1][0] > depth
        if depth <= len(stack):
            stack = stack[:depth]

        full_path = normalize_path("/".join(stack + [name]))

        if is_dir:
            existing_dirs.add(full_path)
            stack.append(name)
        else:
            existing_files.add(full_path)

    return existing_dirs, existing_files

def fetch_remote_tree(
    client: Client, root_path: str, root_dir_id: str | None = None
) -> tuple[dict[str, Directory], set[str]]:
    """Fetch the remote directory tree structure and existing file paths.

    Uses 1-request server-side export_dir to obtain the entire multi-level tree.

    Returns:
        tuple of (existing_dirs, existing_files)
        where existing_dirs maps normalized remote dir path to Directory object,
        and existing_files is a set of normalized remote file paths.
    """
    root_path = normalize_path(root_path)
    existing_dirs: dict[str, Directory] = {}
    existing_files: set[str] = set()

    if not root_dir_id:
        try:
            root_dir_id = client.file._resolve_dir_id(root_path)
        except Exception:
            return existing_dirs, existing_files

    root_stat = Directory(
        id=root_dir_id,
        parent_id="",
        path=root_path,
        name=os.path.basename(root_path),
        pickcode="",
        created_time=None,
        modified_time=None,
        open_time=None,
    )
    existing_dirs[root_path] = root_stat

    try:
        export_result = client.file.export_dir(root_stat, timeout=30.0)
        content = export_result.get("content", "")
        if content:
            parsed_dirs, parsed_files = parse_115_export_tree(content, root_path)
            existing_files.update(parsed_files)
            for d in parsed_dirs:
                if d not in existing_dirs:
                    existing_dirs[d] = Directory(
                        id="",
                        parent_id="",
                        path=d,
                        name=os.path.basename(d),
                        pickcode="",
                        created_time=None,
                        modified_time=None,
                        open_time=None,
                    )
            logger.debug(
                f"Successfully parsed remote tree via export_dir: {len(existing_files)} files found on cloud"
            )
            return existing_dirs, existing_files
        else:
            logger.warning(
                f"export_dir task succeeded but returned empty content: {export_result}"
            )
    except Exception as exc:
        logger.warning(f"export_dir task failed: {exc}", exc_info=True)

    return existing_dirs, existing_files
