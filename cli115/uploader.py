from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import os
from os import PathLike
import re
from typing import Sequence

from blinker import Signal
from pathspec import PathSpec

from cli115.client import Client
from cli115.client.base import MAX_PAGE_SIZE
from cli115.client.models import Directory, File, UploadStatus
from cli115.helpers import format_size, join_path, normalize_path, sha1_file

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
        dedup_by_name: Skip matching paths without verifying their content.
            Directory uploads use one exported tree in this opt-in mode.
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
        dedup_by_name: bool = False,
    ):
        self._client = client
        self.dry_run = dry_run
        self.part_size = part_size
        self.max_workers = max_workers if max_workers and max_workers > 0 else 1
        self.dedup_by_name = dedup_by_name
        self.entries: list[UploadEntry] = []
        self.skipped_files = 0
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
                no_target_dir=no_target_dir,
            )

    def _upload_file(
        self,
        local_path: str,
        remote_path: str,
        *,
        instant_only: int | None,
        part_size: int | None = None,
        no_target_dir: bool = False,
    ) -> File | None:
        if self.dedup_by_name and not os.path.isfile(local_path):
            raise FileNotFoundError(f"local path '{local_path}' is not a regular file")
        remote_path = normalize_path(remote_path)
        parent_path = os.path.dirname(remote_path) or "/"
        parent = Directory(
            id=self._client.file._resolve_dir_id(parent_path),
            parent_id="", path=parent_path, name=os.path.basename(parent_path),
            pickcode="", created_time=None, modified_time=None, open_time=None,
        )
        entry = parent if remote_path == "/" else next(
            (item for item in self._client.file.list(parent)
             if item.name == os.path.basename(remote_path)), None
        )
        # If remote path points to an existing directory, append filename.
        if entry is not None and entry.is_directory:
            if no_target_dir:
                raise IsADirectoryError(f"remote path '{remote_path}' is a directory")
            parent = entry
            file_name = os.path.basename(local_path)
            remote_path = join_path(remote_path, file_name)
            entry = next(
                (item for item in self._client.file.list(parent)
                 if item.name == file_name), None
            )

        if entry is not None:
            _check_same_file(local_path, remote_path, entry, by_name=self.dedup_by_name)
            self.skipped_files += 1
            self.on_entry_added.send(self, entries=[])
            return None if self.dry_run else entry

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
                    # ponytail: external writers still need server-side exclusive
                    # creation to make this preflight check atomic.
                    check_exists=False,
                    dir_id=parent.id,
                    status=upload_entry.status,
                )
            except Exception as exc:
                upload_entry.error = exc
                raise
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
        dir_name = os.path.basename(local_path)
        dest_path = normalize_path(dest_path)
        # If remote_path already ends with the local directory name, don't double it
        if not no_target_dir and os.path.basename(dest_path) != dir_name:
            dest_path = join_path(dest_path, dir_name)

        dest_id: str | None = None
        try:
            dest_id = self._client.file._resolve_dir_id(dest_path)
        except FileNotFoundError:
            try:
                destination = self._client.file.stat(dest_path)
            except FileNotFoundError:
                pass
            else:
                if not destination.is_directory:
                    raise NotADirectoryError(f"remote path '{dest_path}' is a file")
                dest_id = destination.id
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

        # Check all destinations before creating directories or starting workers.
        targets: set[str] = set()
        for _, remote_file in files:
            target = normalize_path(remote_file)
            if target in targets:
                raise FileExistsError(f"multiple local files target '{target}'")
            targets.add(target)
        local_dirs = _collect_dirs(files, dest_path)
        if targets.intersection(local_dirs):
            raise FileExistsError("local file and directory destinations overlap")

        existing_dirs: dict[str, Directory] = {}
        existing_files: dict[str, File] = {}
        existing_names: set[str] = set()

        if dest_id is not None and files:
            logger.debug(
                f"Fetching remote directory tree for destination '{dest_path}' (id={dest_id})..."
            )
            if self.dedup_by_name:
                # ponytail: exported leaves may be empty directories; use the
                # default metadata mode when file type/content must be verified.
                existing_dirs, existing_names = fetch_remote_tree(
                    self._client, dest_path, root_dir_id=dest_id
                )
                existing_names = {normalize_path(path) for path in existing_names}
            else:
                existing_dirs, existing_files = _fetch_remote_entries(
                    self._client, dest_path, dest_id, local_dirs, targets
                )
            logger.debug(
                f"Remote tree fetched: {len(existing_files) + len(existing_names)} existing files found on cloud"
            )

        for directory in local_dirs:
            if directory in existing_files:
                raise NotADirectoryError(f"remote path '{directory}' is a file")

        # Content verification is the default; name-only matching is opt-in.
        needed_files: list[tuple[str, str]] = []
        for lf, rf in files:
            norm_rf = normalize_path(rf)
            existing = existing_files.get(norm_rf) or existing_dirs.get(norm_rf)
            if existing is not None:
                _check_same_file(lf, rf, existing)
            elif norm_rf in existing_names:
                if not os.path.isfile(lf):
                    raise FileNotFoundError(f"local path '{lf}' is not a regular file")
            else:
                needed_files.append((lf, rf))
                continue
            self.skipped_files += 1

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
                if norm_d in existing_dirs or norm_d in existing_names:
                    try:
                        resolved_id = self._client.file._resolve_dir_id(norm_d)
                        dir_id_map[norm_d] = resolved_id
                        dir_id_map[d] = resolved_id
                        continue
                    except FileNotFoundError:
                        if norm_d in existing_names:
                            raise NotADirectoryError(
                                f"remote path '{norm_d}' is not a directory"
                            )
                        pass
                logger.debug(f"Creating remote subdirectory: '{d}'")
                sub_dir = self._client.file.create_directory(d, parents=True)
                dir_id_map[norm_d] = sub_dir.id
                dir_id_map[d] = sub_dir.id
        # 5. Upload files
        # ponytail: one CLI plan owns these targets; external writers need a
        # server-side exclusive-create API to eliminate the remaining race.
        logger.info(
            f"Starting upload of {len(entries)} file(s) with max_workers={max_workers}..."
        )

        def _upload_single_entry(upload_entry: UploadEntry) -> None:
            upload_entry.status.start()
            parent_d = normalize_path(upload_entry.remote_path.rsplit("/", 1)[0])
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
def _check_same_file(
    local_path: str, remote_path: str, existing: Directory | File, *, by_name: bool = False
) -> None:
    if existing.is_directory:
        raise FileExistsError(f"remote path '{remote_path}' is a directory")
    if by_name:
        return
    if existing.sha1 and existing.size == os.path.getsize(local_path):
        with open(local_path, "rb") as source:
            sha1, size = sha1_file(source)
        if size == existing.size and sha1 == existing.sha1.upper():
            return
    raise FileExistsError(
        f"remote file '{remote_path}' exists with different or unverified content"
    )


def _fetch_remote_entries(
    client: Client, root_path: str, root_id: str, local_dirs: set[str], targets: set[str]
) -> tuple[dict[str, Directory], dict[str, File]]:
    root = Directory(
        id=root_id, parent_id="", path=root_path, name=os.path.basename(root_path),
        pickcode="", created_time=None, modified_time=None, open_time=None,
    )
    directories = {root_path: root}
    files: dict[str, File] = {}
    pending = [(root_path, root)]
    while pending:
        parent_path, parent = pending.pop()
        # list(Directory) reuses known IDs and fetches each relevant page once.
        for entry in client.file.list(parent, page_size=MAX_PAGE_SIZE):
            path = normalize_path(join_path(parent_path, entry.name))
            if path not in targets and path not in local_dirs:
                continue
            previous = directories.get(path) or files.get(path)
            if previous is not None:
                if previous.id == entry.id and previous.is_directory == entry.is_directory:
                    continue
                raise FileExistsError(f"multiple remote entries share path '{path}'")
            if entry.is_directory:
                directories[path] = entry
                if path in local_dirs:
                    pending.append((path, entry))
            else:
                files[path] = entry
    return directories, files


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
    dest_path = normalize_path(dest_path)
    for _local, remote in files:
        parent = normalize_path(remote).rsplit("/", 1)[0]
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
        raise ValueError("directory export is empty")

    parsed_lines: list[tuple[int, str]] = []
    official_line = re.compile(r"^([ \t|]*)\|(?:——|-)[ \t]?(.*)$")
    is_115_official = any(official_line.match(line) for line in lines[:30])

    if is_115_official:
        for line in lines:
            raw = line.rstrip("\r\n")
            if not raw.strip() or "|" not in raw:
                continue
            match = official_line.match(raw)
            if match is None:
                raise ValueError("unrecognized directory export format")
            prefix, name = match.groups()
            if not name:
                continue
            depth = prefix.count("|")
            parsed_lines.append((depth, name))
    else:
        for line_idx, line in enumerate(lines):
            raw = line.rstrip("\r\n")
            if not raw.strip():
                continue
            match = re.match(r"^([ \t│|]*)(?:├──|└──)[ \t]?(.*)$", raw)
            if match is None:
                if line_idx == 0 or re.fullmatch(
                    r"\d+ directories?, \d+ files?", raw.strip()
                ):
                    continue
                raise ValueError("unrecognized directory export format")
            prefix, name = match.groups()
            if not name:
                continue
            depth = len(prefix.expandtabs(4)) // 4 + 1
            parsed_lines.append((depth, name))

    if not parsed_lines:
        if content.strip() in (root_dest_path.rsplit("/", 1)[-1], ".", "/"):
            return {root_dest_path}, set()
        raise ValueError("unrecognized directory export format")

    if is_115_official:
        if parsed_lines[0][0] != 0:
            raise ValueError("invalid directory export root")
        if root_dest_path != "/":
            root_name = root_dest_path.rsplit("/", 1)[-1]
            if (
                parsed_lines[0] == (0, "根目录")
                and parsed_lines[1:2] == [(1, root_name)]
                and all(depth >= 2 for depth, _ in parsed_lines[2:])
            ):
                parsed_lines = [(depth - 1, name) for depth, name in parsed_lines[1:]]
            if parsed_lines[0] != (0, root_name):
                raise ValueError("export root does not match requested directory")

    stack = [root_dest_path]
    existing_dirs = {root_dest_path}
    existing_files: set[str] = set()

    total = len(parsed_lines)
    for i in range(total):
        depth, name = parsed_lines[i]
        if depth == 0:
            if i != 0:
                raise ValueError("directory export contains multiple roots")
            continue

        if depth > len(stack):
            raise ValueError("invalid directory export indentation")

        # ponytail: text cannot identify empty directories; use typed listings if needed.
        is_dir = i + 1 < total and parsed_lines[i + 1][0] > depth
        if depth <= len(stack):
            stack = stack[:depth]

        full_path = join_path(stack[0], *stack[1:], name)

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
        except FileNotFoundError:
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
        export_result = client.file.export_dir(root_stat, timeout=120.0)
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
            raise ValueError("directory export returned empty content")
    except Exception as exc:
        logger.error(f"export_dir task failed: {exc}", exc_info=True)
        raise OSError(
            f"Failed to fetch cloud directory tree for '{root_path}': {exc}. "
            "Aborting upload to avoid duplicate uploads."
        ) from exc
    return existing_dirs, existing_files
