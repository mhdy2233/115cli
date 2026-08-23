"""Upload command."""

from __future__ import annotations

import argparse
import os
import threading
import time
from tqdm import tqdm

from cli115.client.models import Progress
from cli115.cmds.base import BaseCommand, WorkerCommand
from cli115.cmds.formatter import PairFormatterMixin, format_entry
from cli115.helpers import format_size, parse_size
from cli115.uploader import UploadEntry, Uploader


class UploadCommand(PairFormatterMixin, WorkerCommand, BaseCommand):
    """Upload a local file or directory to the remote path."""

    uploader: Uploader | None = None

    def register(self, parser: argparse.ArgumentParser) -> None:
        super().register(parser)
        parser.add_argument("local_path", help="local file or directory path")
        parser.add_argument("remote_path", help="remote destination path")
        parser.add_argument(
            "--plan",
            action="store_true",
            default=False,
            help="Show planned files before uploading",
        )
        parser.add_argument(
            "--instant-only",
            type=parse_size,
            default=None,
            metavar="SIZE",
            help=(
                "Force instant (hash-based) upload for files at or above SIZE "
                "(e.g. '100MB', '1GB').  Raises an error if the server does not "
                "have a matching copy.  Values below 2 MB are ignored."
            ),
        )
        parser.add_argument(
            "--part-size",
            type=parse_size,
            default=None,
            metavar="SIZE",
            help=(
                "Part size for multipart uploads (e.g. '16MB', '32MB', '64MB'). "
                "Defaults to config value or 16 MB."
            ),
        )
        parser.add_argument(
            "-j",
            "--threads",
            "--max-workers",
            dest="max_workers",
            type=int,
            default=None,
            metavar="N",
            help="Number of concurrent file uploads for directories (default: 1)",
        )
        parser.add_argument(
            "--include",
            action="append",
            default=None,
            metavar="PATTERN",
            help=(
                "Glob pattern for files to include when uploading a directory "
                "(may be repeated; only matching files are uploaded)"
            ),
        )
        parser.add_argument(
            "--exclude",
            action="append",
            default=None,
            metavar="PATTERN",
            help=(
                "Glob pattern for files to exclude when uploading a directory "
                "(may be repeated; matching files are skipped)"
            ),
        )
        parser.add_argument(
            "-T",
            "--no-target-directory",
            action="store_true",
            default=False,
            help=(
                "Treat remote_path as the exact destination rather than a "
                "directory to upload into (never append the local name)"
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Only show files that would be uploaded without uploading",
        )
        parser.add_argument(
            "-s",
            "--silent",
            action="store_true",
            default=False,
            help="Do not report progress, only print the final result",
        )

    def execute(self, args: argparse.Namespace) -> None:
        part_size = args.part_size
        if part_size is None and self.cfg and "upload" in self.cfg:
            part_size_str = self.cfg.get("upload", "part_size", fallback=None)
            if part_size_str:
                part_size = parse_size(part_size_str)

        max_workers = args.max_workers
        if max_workers is None and self.cfg and "upload" in self.cfg:
            max_workers = self.cfg.getint("upload", "max_workers", fallback=1)
        if max_workers is None or max_workers < 1:
            max_workers = 1

        self.uploader = Uploader(
            self._create_client(),
            dry_run=args.dry_run,
            part_size=part_size,
            max_workers=max_workers,
        )

        with UploadProgress(
            self.uploader,
            show_plan=args.plan or args.dry_run,
            show_progress=not args.silent and not args.dry_run,
        ):
            result = self.run_worker(args)

        failed_entries = [
            entry for entry in self.uploader.entries if entry.error is not None
        ]
        if failed_entries:
            self.warn("{0} file(s) failed to upload".format(len(failed_entries)))
        for entry in failed_entries:
            self.warn(
                "- {0} -> {1}: {2}".format(
                    os.fspath(entry.local_path),
                    entry.remote_path,
                    entry.error,
                )
            )

        if result:
            self.output(format_entry(result), args)

    def worker(self, args):
        return self.uploader.upload(
            args.local_path,
            args.remote_path,
            instant_only=args.instant_only,
            include=args.include,
            exclude=args.exclude,
            no_target_dir=args.no_target_directory,
        )


class UploadProgress:
    def __init__(
        self,
        uploader: Uploader,
        *,
        show_plan: bool = False,
        show_progress: bool = True,
    ):
        self.uploader = uploader
        self.show_plan = show_plan
        self.show_progress = show_progress
        self._lock = threading.Lock()

        self.bar: tqdm | None = None
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.total_files = 0
        self.total_size = 0
        self.completed_files = 0
        self.instant_count = 0

        self._entry_transferred: dict[UploadEntry, int] = {}
        self._active_entries: dict[UploadEntry, str] = {}

    def _update_description(self) -> None:
        """Update progress bar description string. Must be called under self._lock."""
        if self.bar is None:
            return

        if self.uploader.max_workers == 1 and self._active_entries:
            entry, msg = next(iter(self._active_entries.items()))
            fname = os.path.basename(os.fspath(entry.local_path))
            if msg and msg != "uploading":
                desc = f"[{self.completed_files}/{self.total_files}] {fname} ({msg})"
            else:
                desc = f"[{self.completed_files}/{self.total_files}] {fname}"
        elif len(self._active_entries) > 0:
            active_names = [
                os.path.basename(os.fspath(e.local_path))
                for e in list(self._active_entries.keys())
            ]
            names_str = ", ".join(active_names[:3])
            if len(active_names) > 3:
                names_str += f"... (+{len(active_names) - 3})"
            desc = f"[{self.completed_files}/{self.total_files} done, {len(self._active_entries)} active] {names_str}"
        else:
            desc = f"[{self.completed_files}/{self.total_files} done]"

        if len(desc) > 60:
            desc = desc[:57] + "..."
        self.bar.set_description_str(desc, refresh=False)

    def init(self):
        self.uploader.on_entry_added.connect(self.on_added)
        self.started_at = time.monotonic()

    def close(self):
        self.ended_at = time.monotonic()
        if self.bar is not None:
            self.bar.close()
            print()

    def report(self):
        if self.started_at is None or self.ended_at is None:
            return

        self.ended_at = time.monotonic()
        elapsed = self.ended_at - self.started_at
        tqdm.write(
            "Upload finished in {0:.1f}s: {1} total, {2} of {3} files instantly uploaded".format(
                elapsed,
                format_size(self.total_size),
                self.instant_count,
                self.total_files,
            )
        )

    def on_added(self, sender, **kw):
        entries: list[UploadEntry] = kw["entries"]
        if self.show_plan:
            for idx, entry in enumerate(entries):
                print(
                    "{0}. {1} -> {2} ({3})".format(
                        idx,
                        os.fspath(entry.local_path),
                        entry.remote_path,
                        format_size(entry.size),
                    )
                )

        if not self.show_progress:
            return

        self.total_files = len(entries)
        self.total_size = max(sum(e.size for e in entries), 1)
        self.bar = tqdm(
            total=self.total_size,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            dynamic_ncols=True,
            leave=False,
            desc=f"[0/{self.total_files}]",
            bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
        )

        for entry in entries:
            self.connect_message_listener(entry)
            self.connect_upload_listener(entry)
            self.connect_complete_listener(entry)

    def connect_message_listener(self, entry: UploadEntry):
        def listener(sender, message: str) -> None:
            with self._lock:
                self._active_entries[entry] = message
                self._update_description()
                if self.bar is not None:
                    self.bar.refresh()

        entry.status.on_message.connect(listener, weak=False)

    def connect_upload_listener(self, entry: UploadEntry):
        def listener(sender, progress: Progress) -> None:
            with self._lock:
                self._active_entries[entry] = "uploading"
                self._update_description()

            def on_progress(sender, delta: int, new: int, old: int, completed: bool):
                with self._lock:
                    prev = self._entry_transferred.get(entry, 0)
                    chunk_delta = new - prev
                    if chunk_delta > 0:
                        self._entry_transferred[entry] = new
                        if self.bar is not None:
                            self.bar.update(chunk_delta)

            progress.on_change.connect(on_progress, weak=False)

        entry.status.on_upload.connect(listener, weak=False)

    def connect_complete_listener(self, entry: UploadEntry):
        def listener(sender) -> None:
            with self._lock:
                self.completed_files += 1
                self._active_entries.pop(entry, None)

                prev = self._entry_transferred.get(entry, 0)
                remaining_bytes = entry.size - prev

                if entry.status.is_instant_uploaded:
                    self.instant_count += 1

                if remaining_bytes > 0:
                    self._entry_transferred[entry] = entry.size
                    if self.bar is not None:
                        self.bar.update(remaining_bytes)

                self._update_description()
                if self.bar is not None:
                    self.bar.refresh()

        entry.status.on_complete.connect(listener, weak=False)

    def __enter__(self):
        self.init()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        if exc_type is None and self.show_progress:
            self.report()
