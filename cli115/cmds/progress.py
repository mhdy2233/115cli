"""Shared upload and download progress, written only to stderr."""

from __future__ import annotations

from functools import partial
import os
import sys
import threading
import time

from tqdm import tqdm

from cli115.helpers import format_size


class TransferProgress:
    def __init__(
        self, worker, direction: str, *, show_plan=False, show_progress=True,
        dynamic=True,
    ):
        self.worker = worker
        self.direction = direction
        self.show_plan = show_plan
        self.show_progress = show_progress
        self.dynamic = dynamic
        self.started_at = None
        self.ended_at = None
        self.total_files = 0
        self.total_size = 0
        self.completed_files = 0
        self.instant_count = 0
        self.skipped_files = 0
        self.failed_files = 0
        self.total_transferred = 0
        self.overall_bar = None
        self._bars = []
        self._active = {}
        self._entries = set()
        self._done = set()
        self._connections = []
        self._lock = threading.RLock()
        self._closed = False
        self._error = None

    def _connect(self, signal, callback):
        signal.connect(callback, weak=False)
        self._connections.append((signal, callback))

    def init(self):
        self.started_at = time.monotonic()
        self._connect(self.worker.on_entry_added, self.on_added)
        if (
            self.show_progress and self.dynamic and not self.worker.dry_run
            and sys.stderr.isatty()
        ):
            self.overall_bar = tqdm(
                total=None, desc=f"{self.direction}: scanning/comparing",
                unit="file", position=0, file=sys.stderr, leave=False,
                dynamic_ncols=True,
            )

    def _size(self, entry):
        return entry.size if self.direction == "Upload" else entry.remote_entry.size

    def on_added(self, sender, *, entries):
        with self._lock:
            if self._closed:
                return
            for entry in entries:
                if entry in self._entries:
                    continue
                self._entries.add(entry)
                self.total_files += 1
                self.total_size += self._size(entry)
                if self.show_plan and self.show_progress:
                    source, target = (
                        (os.fspath(entry.local_path), entry.remote_path)
                        if self.direction == "Upload"
                        else (entry.remote_entry.path, os.fspath(entry.local_path))
                    )
                    tqdm.write(
                        f"{self.total_files}. {source} -> {target} "
                        f"({format_size(self._size(entry))})", file=sys.stderr,
                    )
                status = entry.status
                self._connect(status.on_message, partial(self._message, entry))
                self._connect(status.on_complete, partial(self._complete, entry))
                if self.direction == "Upload":
                    self._connect(status.on_start, partial(self._start, entry))
                    self._connect(status.on_upload, partial(self._transfer, entry, True))
                else:
                    self._connect(status.on_download, partial(self._transfer, entry, True))
                    self._connect(status.on_integrity_check, partial(self._transfer, entry, False))
            if self.overall_bar is not None:
                self.overall_bar.total = self.total_files
                self.overall_bar.set_description_str(self.direction, refresh=False)
                self._refresh()

    def _start(self, entry, sender=None):
        with self._lock:
            if self._closed or entry.status.is_completed or entry.error is not None:
                return
            for previous in list(self._active):
                if previous.error is not None:
                    self._complete(previous, previous.status)
            if entry not in self._active and self.overall_bar is not None:
                used = set(self._active.values())
                bar = next((bar for bar in self._bars if bar not in used), None)
                if bar is None:
                    bar = tqdm(
                        total=self._size(entry), position=len(self._bars) + 1,
                        file=sys.stderr, leave=False, dynamic_ncols=True,
                        unit="B", unit_scale=True, unit_divisor=1024,
                    )
                    self._bars.append(bar)
                self._active[entry] = bar
                bar.bar_format = "{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}]"
                bar.set_description_str(os.path.basename(os.fspath(entry.local_path)), refresh=False)
                bar.reset(total=self._size(entry))

    def _message(self, entry, sender, *, message):
        with self._lock:
            self._start(entry)
            bar = self._active.get(entry)
            if bar is not None:
                if message.startswith("file sha1 calculated:"):
                    message = "file hash calculated"
                elif message.startswith("instant upload failed:"):
                    message = "instant upload unavailable"
                name = os.path.basename(os.fspath(entry.local_path))
                bar.set_description_str(f"{name} ({message})")

    def _transfer(self, entry, network, sender, *, progress):
        with self._lock:
            if self._closed or entry.status.is_completed:
                return
            self._start(entry)
            bar = self._active.get(entry)
            if bar is not None:
                bar.bar_format = (
                    "{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
                    + ("[{elapsed}<{remaining}, {rate_fmt}]" if network else "[{elapsed}]")
                )
                bar.reset(total=progress.total_bytes)
            self._connect(progress.on_change, partial(self._advance, entry, network))

    def _advance(self, entry, network, sender, *, delta, new, **kw):
        with self._lock:
            if self._closed:
                return
            if network:
                self.total_transferred += max(0, delta)
            bar = self._active.get(entry)
            if bar is not None:
                bar.update(new - bar.n)
            self._refresh()

    def _complete(self, entry, sender):
        with self._lock:
            if self._closed or entry in self._done:
                return
            self._done.add(entry)
            bar = self._active.pop(entry, None)
            if bar is not None:
                bar.clear()
                bar.bar_format = "{desc}"
                bar.set_description_str("", refresh=False)
            self._refresh()

    def _refresh(self):
        if self.overall_bar is not None:
            elapsed = max(time.monotonic() - self.started_at, 0.001)
            self.overall_bar.n = len(self._done)
            self.overall_bar.set_postfix_str(
                f"{format_size(self.total_transferred)} transferred, "
                f"{format_size(self.total_transferred / elapsed)}/s", refresh=False,
            )
            self.overall_bar.update(0)

    def _summarize(self):
        entries = self.worker.entries
        self.failed_files = sum(entry.error is not None for entry in entries)
        self.skipped_files = (
            self.worker.skipped_files if self.direction == "Upload"
            else sum(entry.status.is_skipped for entry in entries if entry.error is None)
        )
        self.completed_files = sum(
            entry.status.is_completed and entry.error is None
            and not getattr(entry.status, "is_skipped", False)
            for entry in entries
        )
        self.instant_count = sum(
            bool(getattr(entry.status, "is_instant_uploaded", False))
            and entry.error is None for entry in entries
        )

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.ended_at = time.monotonic()
            for signal, callback in self._connections:
                signal.disconnect(callback)
            self._connections.clear()
            for bar in reversed(self._bars):
                bar.close()
            if self.overall_bar is not None:
                self.overall_bar.close()
            self._active.clear()
            self._summarize()

    def report(self):
        if self.started_at is None or self.ended_at is None:
            return
        if self._error is not None and not self.worker.entries:
            print(f"{self.direction} aborted before transfer", file=sys.stderr)
            return
        self._summarize()
        if self.worker.dry_run:
            print(
                f"{self.direction} plan: {self.total_files} files "
                f"({format_size(self.total_size)}), {self.skipped_files} skipped; "
                "dry run, no files transferred", file=sys.stderr,
            )
            return
        elapsed = max(self.ended_at - self.started_at, 0.001)
        total = self.total_files + (self.skipped_files if self.direction == "Upload" else 0)
        pending = max(0, total - self.completed_files - self.skipped_files - self.failed_files)
        verb = "Uploaded" if self.direction == "Upload" else "Downloaded"
        print(
            f"{verb}: {self.completed_files} / {total} files, "
            f"{self.failed_files} failed, {self.skipped_files} skipped, "
            f"{self.instant_count} instant, {pending} pending\n"
            f"Transferred: {format_size(self.total_transferred)}, "
            f"{format_size(self.total_transferred / elapsed)}/s; elapsed {elapsed:.1f}s",
            file=sys.stderr,
        )

    def __enter__(self):
        self.init()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._error = exc_val
        self.close()
        if self.show_progress:
            self.report()
