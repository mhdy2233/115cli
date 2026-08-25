from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from os import PathLike

from blinker import Signal
from pathspec import PathSpec

from cli115.client import Client
from cli115.client.models import Directory, File, UploadStatus
from cli115.helpers import join_path, normalize_path

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
        try:
            entry = self._client.file.stat(dest_path)
            if not entry.is_directory:
                raise FileExistsError(
                    f"cannot upload directory to a file path: {dest_path}"
                )
            # Remote exists as a directory: create a subdirectory with the local dir name.
            if not no_target_dir:
                dir_name = os.path.basename(local_path)
                dest_path = join_path(dest_path, dir_name)
        except FileNotFoundError:
            pass

        include_spec = PathSpec.from_lines("gitignore", include) if include else None
        exclude_spec = PathSpec.from_lines("gitignore", exclude) if exclude else None

        # Collect files to upload
        files = _collect_files(
            local_path,
            dest_path,
            include=include_spec,
            exclude=exclude_spec,
        )
        dirs = _collect_dirs(files, dest_path)

        # 1. Recursively construct remote directory tree to fetch existing directories and files
        existing_dirs, existing_files = fetch_remote_tree(self._client, dest_path)

        # 2. Perform in-memory comparison: ONLY keep files that do NOT exist on cloud
        needed_files: list[tuple[str, str]] = []
        for lf, rf in files:
            norm_rf = normalize_path(rf)
            if norm_rf in existing_files:
                # File already exists on cloud, skip
                continue
            needed_files.append((lf, rf))

        # 3. Only queue files that actually need to be uploaded
        entries = [UploadEntry(lf, rf) for lf, rf in needed_files]
        self.entries.extend(entries)
        self.on_entry_added.send(self, entries=entries)

        norm_dest_path = normalize_path(dest_path)
        if self.dry_run or not entries:
            return existing_dirs.get(norm_dest_path)

        # 4. ONLY create/resolve directories for the needed files (grouped by directory)
        needed_dirs = _collect_dirs(needed_files, dest_path)
        dir_id_map: dict[str, str] = {d: entry.id for d, entry in existing_dirs.items()}

        dest_dir = existing_dirs.get(norm_dest_path)
        if dest_dir is None:
            dest_dir = self._client.file.create_directory(dest_path, parents=True)
            dir_id_map[norm_dest_path] = dest_dir.id
            dir_id_map[dest_path] = dest_dir.id

        for d in sorted(needed_dirs):
            norm_d = normalize_path(d)
            if norm_d not in dir_id_map and d not in dir_id_map:
                sub_dir = self._client.file.create_directory(d, parents=True)
                dir_id_map[norm_d] = sub_dir.id
                dir_id_map[d] = sub_dir.id
        def _upload_single_entry(upload_entry: UploadEntry) -> None:
            upload_entry.status.start()
            parent_d = upload_entry.remote_path.rsplit("/", 1)[0]
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
            except Exception as exc:
                upload_entry.error = exc
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


def fetch_remote_tree(
    client: Client, root_path: str
) -> tuple[dict[str, Directory], set[str]]:
    """Recursively fetch the remote directory tree structure and existing file paths.

    Returns:
        tuple of (existing_dirs, existing_files)
        where existing_dirs maps normalized remote dir path to Directory object,
        and existing_files is a set of normalized remote file paths.
    """
    root_path = normalize_path(root_path)
    existing_dirs: dict[str, Directory] = {}
    existing_files: set[str] = set()

    try:
        root_stat = client.file.stat(root_path)
        if not root_stat.is_directory:
            return existing_dirs, existing_files
        root_stat.path = root_path
        existing_dirs[root_path] = root_stat
    except Exception:
        return existing_dirs, existing_files

    def _traverse(current_dir: Directory) -> None:
        try:
            items = list(client.file.list(current_dir))
        except Exception:
            return

        for item in items:
            item_path = normalize_path(
                item.path if item.path else join_path(current_dir.path, item.name)
            )
            if item.is_directory:
                item.path = item_path
                existing_dirs[item_path] = item
                _traverse(item)
            else:
                existing_files.add(item_path)

    _traverse(existing_dirs[root_path])
    return existing_dirs, existing_files
