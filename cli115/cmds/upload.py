"""Upload command."""

from __future__ import annotations

import argparse
from collections import deque
import logging
import os
import shutil
import sys
import threading
import time
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
            "-v",
            "--verbose",
            "--debug",
            dest="debug",
            action="store_true",
            default=False,
            help="Enable debug logging to display detailed upload operations",
        )
        parser.add_argument(
            "-s",
            "--silent",
            action="store_true",
            default=False,
            help="Do not report progress, only print the final result",
        )

    def execute(self, args: argparse.Namespace) -> None:
        if getattr(args, "debug", False):
            logging.basicConfig(
                level=logging.DEBUG,
                format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
                datefmt="%H:%M:%S",
                force=True,
            )

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
            show_progress=not args.silent and not args.dry_run and not getattr(args, "debug", False),
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


def _format_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds > 86400 * 7:
        return "-"
    sec_int = int(seconds)
    if sec_int < 60:
        return f"{sec_int}s"
    minutes, sec = divmod(sec_int, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _make_bar(percent: float, width: int = 12) -> str:
    filled = int(round(width * percent / 100))
    filled = max(0, min(width, filled))
    return "█" * filled + " " * (width - filled)


class ActiveFileTracker:
    """Tracks transferred bytes and speed for an active file upload."""

    def __init__(self, entry: UploadEntry) -> None:
        self.entry = entry
        self.transferred = 0
        self.msg = "starting"
        self.history: deque[tuple[float, int]] = deque(maxlen=10)
        self.history.append((time.monotonic(), 0))

    def update(self, new_bytes: int) -> None:
        self.transferred = new_bytes
        self.history.append((time.monotonic(), new_bytes))

    def get_speed(self) -> float:
        if len(self.history) < 2:
            return 0.0
        t0, b0 = self.history[0]
        t1, b1 = self.history[-1]
        dt = t1 - t0
        return max(0.0, (b1 - b0) / dt) if dt > 0 else 0.0


class UploadProgress:
    """rclone-style multi-threaded upload progress dashboard."""

    def __init__(
        self,
        uploader: Uploader,
        *,
        show_plan: bool = False,
        show_progress: bool = True,
    ):
        if sys.platform == "win32":
            os.system("")  # Enable ANSI terminal escape sequences on Windows

        self.uploader = uploader
        self.show_plan = show_plan
        self.show_progress = show_progress
        self._lock = threading.Lock()

        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.total_files = 0
        self.total_size = 0
        self.completed_files = 0
        self.instant_count = 0
        self.total_transferred = 0

        self._entry_transferred: dict[UploadEntry, int] = {}
        self._active_trackers: dict[UploadEntry, ActiveFileTracker] = {}
        self._overall_history: deque[tuple[float, int]] = deque(maxlen=15)
        self._rendered_lines = 0
        self._running = False
        self._render_thread: threading.Thread | None = None

    def init(self) -> None:
        self.uploader.on_entry_added.connect(self.on_added)
        self.started_at = time.monotonic()
        self._overall_history.append((self.started_at, 0))

    def on_added(self, sender, **kw) -> None:
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
        self.total_files = len(entries)
        self.total_size = max(sum(e.size for e in entries), 1)

        for entry in entries:
            self.connect_start_listener(entry)
            self.connect_message_listener(entry)
            self.connect_upload_listener(entry)
            self.connect_complete_listener(entry)

        if self.show_progress:
            self._running = True
            self._render_thread = threading.Thread(
                target=self._render_loop, daemon=True
            )
            self._render_thread.start()
    def connect_start_listener(self, entry: UploadEntry) -> None:
        def listener(sender) -> None:
            with self._lock:
                if entry not in self._active_trackers:
                    self._active_trackers[entry] = ActiveFileTracker(entry)

        entry.status.on_start.connect(listener, weak=False)
    def connect_message_listener(self, entry: UploadEntry) -> None:
        def listener(sender, message: str) -> None:
            with self._lock:
                if entry in self._active_trackers:
                    self._active_trackers[entry].msg = message
                else:
                    tracker = ActiveFileTracker(entry)
                    tracker.msg = message
                    self._active_trackers[entry] = tracker

        entry.status.on_message.connect(listener, weak=False)

    def connect_upload_listener(self, entry: UploadEntry) -> None:
        def listener(sender, progress: Progress) -> None:
            with self._lock:
                if entry not in self._active_trackers:
                    self._active_trackers[entry] = ActiveFileTracker(entry)
                self._active_trackers[entry].msg = "uploading"

            def on_progress(sender, delta: int, new: int, old: int, completed: bool) -> None:
                with self._lock:
                    prev = self._entry_transferred.get(entry, 0)
                    chunk_delta = new - prev
                    if chunk_delta > 0:
                        self._entry_transferred[entry] = new
                        self.total_transferred += chunk_delta
                        self._overall_history.append(
                            (time.monotonic(), self.total_transferred)
                        )
                    if entry in self._active_trackers:
                        self._active_trackers[entry].update(new)

            progress.on_change.connect(on_progress, weak=False)

        entry.status.on_upload.connect(listener, weak=False)

    def connect_complete_listener(self, entry: UploadEntry) -> None:
        def listener(sender) -> None:
            with self._lock:
                self.completed_files += 1
                self._active_trackers.pop(entry, None)
                prev = self._entry_transferred.get(entry, 0)
                remaining_bytes = entry.size - prev

                if entry.status.is_instant_uploaded:
                    self.instant_count += 1

                if remaining_bytes > 0:
                    self._entry_transferred[entry] = entry.size
                    self.total_transferred += remaining_bytes
                    self._overall_history.append(
                        (time.monotonic(), self.total_transferred)
                    )

        entry.status.on_complete.connect(listener, weak=False)

    def _get_overall_speed(self) -> float:
        if len(self._overall_history) < 2:
            now = time.monotonic()
            dt = now - (self.started_at or now)
            return (self.total_transferred / dt) if dt > 0 else 0.0
        t0, b0 = self._overall_history[0]
        t1, b1 = self._overall_history[-1]
        dt = t1 - t0
        return max(0.0, (b1 - b0) / dt) if dt > 0 else 0.0

    def _build_lines(self) -> list[str]:
        with self._lock:
            now = time.monotonic()
            elapsed = now - (self.started_at or now)
            speed = self._get_overall_speed()
            percent = min(100.0, (self.total_transferred / self.total_size) * 100)
            file_percent = min(
                100.0, (self.completed_files / max(self.total_files, 1)) * 100
            )
            remaining_bytes = max(0, self.total_size - self.total_transferred)
            eta = (remaining_bytes / speed) if speed > 0 else None

            lines = [
                f"Transferred:   {format_size(self.total_transferred):>10} / {format_size(self.total_size)}, {percent:3.0f}%, {format_size(speed)}/s, ETA {_format_eta(eta)}",
                f"Transferred:   {self.completed_files:>10} / {self.total_files} files, {file_percent:3.0f}%",
                f"Elapsed time:  {_format_duration(elapsed):>10}",
            ]

            if self._active_trackers:
                lines.append("Transferring:")
                for entry, tracker in list(self._active_trackers.items()):
                    fname = os.path.basename(os.fspath(entry.local_path))
                    f_size = max(entry.size, 1)
                    f_percent = min(100.0, (tracker.transferred / f_size) * 100)
                    f_speed = tracker.get_speed()
                    f_remaining = max(0, entry.size - tracker.transferred)
                    f_eta = (f_remaining / f_speed) if f_speed > 0 else None
                    bar = _make_bar(f_percent, width=12)
                    if tracker.msg and tracker.msg != "uploading":
                        lines.append(
                            f" * {fname}: {f_percent:3.0f}% [{bar}] {format_size(tracker.transferred)} / {format_size(entry.size)} ({tracker.msg})"
                        )
                    else:
                        lines.append(
                            f" * {fname}: {f_percent:3.0f}% [{bar}] {format_size(tracker.transferred)} / {format_size(entry.size)}, {format_size(f_speed)}/s, {_format_eta(f_eta)}"
                        )
            return lines

    def _render_loop(self) -> None:
        while self._running:
            self._render_frame()
            time.sleep(0.2)

    def _render_frame(self) -> None:
        if not self.show_progress or not sys.stdout.isatty():
            return
        lines = self._build_lines()
        term_width = shutil.get_terminal_size((80, 24)).columns

        formatted_lines = []
        for line in lines:
            if len(line) > term_width:
                formatted_lines.append(line[: term_width - 3] + "...")
            else:
                formatted_lines.append(line)

        out = []
        if self._rendered_lines > 0:
            out.append(f"\033[{self._rendered_lines}F")
        for line in formatted_lines:
            out.append(f"\r\033[K{line}\n")

        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self._rendered_lines = len(formatted_lines)

    def close(self) -> None:
        self._running = False
        if self._render_thread and self._render_thread.is_alive():
            self._render_thread.join(timeout=1.0)
        self.ended_at = time.monotonic()

        if self._rendered_lines > 0 and sys.stdout.isatty():
            out = [f"\033[{self._rendered_lines}F"]
            for _ in range(self._rendered_lines):
                out.append("\r\033[K\n")
            out.append(f"\033[{self._rendered_lines}F")
            sys.stdout.write("".join(out))
            sys.stdout.flush()
            self._rendered_lines = 0

    def report(self) -> None:
        if self.started_at is None or self.ended_at is None:
            return
        elapsed = max(0.001, self.ended_at - self.started_at)
        if self.total_files == 0:
            print(f"Elapsed time:  {_format_duration(elapsed)}")
            print("All files are already up to date on cloud: 0 files to upload.")
            return

        speed = self.total_size / elapsed
        print(
            f"Transferred:   {format_size(self.total_size)} / {format_size(self.total_size)}, 100%, {format_size(speed)}/s, ETA 0s\n"
            f"Transferred:   {self.completed_files} / {self.total_files} files, 100%\n"
            f"Elapsed time:  {_format_duration(elapsed)}\n"
            f"Upload finished in {elapsed:.1f}s: {format_size(self.total_size)} total, {self.instant_count} of {self.total_files} files instantly uploaded"
        )
    def __enter__(self):
        self.init()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        if exc_type is None and self.show_progress:
            self.report()
