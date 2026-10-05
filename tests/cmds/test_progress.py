from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Barrier
from types import SimpleNamespace

from blinker import Signal
import pytest

from cli115.cmds.fetch import FetchProgress
from cli115.cmds.upload import UploadProgress
from cli115.fetcher import FetchEntry
from cli115.uploader import UploadEntry
from tests.client.conftest import make_file


def make_worker():
    return SimpleNamespace(
        entries=[], skipped_files=0, dry_run=False, on_entry_added=Signal(),
    )


def make_entry(kind, name):
    if kind == "upload":
        entry = UploadEntry(name, f"/remote/{name}")
        entry.size = 100
        return entry
    return FetchEntry(make_file(name=name, path=f"/remote/{name}", size=100), name)


def add(worker, *entries):
    worker.entries.extend(entries)
    worker.on_entry_added.send(worker, entries=list(entries))


def transfer(entry, kind):
    return (
        entry.status.start_upload(100) if kind == "upload"
        else entry.status.start_download(100)
    )


def complete(entry, kind):
    if kind == "upload":
        entry.status._complete()
    else:
        entry.status.complete()


class TestTransferProgress:
    @pytest.mark.parametrize("kind,progress_class", [("upload", UploadProgress), ("fetch", FetchProgress)])
    def test_concurrent_entries_reuse_tty_rows(self, kind, progress_class, monkeypatch, capsys):
        terminal = StringIO()
        monkeypatch.setattr(terminal, "isatty", lambda: True)
        monkeypatch.setattr("sys.stderr", terminal)
        worker = make_worker()
        first = make_entry(kind, "first.txt")
        second = make_entry(kind, "second.txt")
        third = make_entry(kind, "third.txt")
        barrier = Barrier(3)

        def run(entry):
            with transfer(entry, kind) as progress:
                progress.update(40)
                barrier.wait(timeout=5)
                barrier.wait(timeout=5)
                progress.update(60)
            complete(entry, kind)
            entry.status.set_message("late completion message")

        with progress_class(worker) as display:
            overall = display.overall_bar
            add(worker, first)
            add(worker, second)
            worker.on_entry_added.send(worker, entries=[first])
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run, entry) for entry in (first, second)]
                barrier.wait(timeout=5)
                assert set(display._active) == {first, second}
                assert display.total_transferred == 80
                assert all(bar.n == 40 for bar in display._active.values())
                for message, expected in (
                    ("file sha1 calculated: " + "a" * 40, "file hash calculated"),
                    ("instant upload failed: long server response", "instant upload unavailable"),
                    ("checking file: first.txt", "checking file: first.txt"),
                ):
                    first.status.set_message(message)
                    assert display._active[first].desc == f"first.txt ({expected})"
                barrier.wait(timeout=5)
                for future in futures:
                    future.result(timeout=5)
            assert not display._active
            add(worker, third)
            with transfer(third, kind) as progress:
                progress.update(100)
            complete(third, kind)
            assert display.overall_bar is overall
            assert len(display._bars) == 2
        assert display.total_files == display.completed_files == 3
        assert display.total_transferred == display.total_size == 300
        assert not worker.on_entry_added.receivers
        assert all(not entry.status.on_message.receivers for entry in worker.entries)
        assert "first.txt" in terminal.getvalue()
        assert "second.txt" in terminal.getvalue()
        assert "3 / 3 files, 0 failed" in terminal.getvalue()
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("kind,progress_class", [("upload", UploadProgress), ("fetch", FetchProgress)])
    @pytest.mark.parametrize("silent", [False, True])
    def test_final_counts_and_actual_network_bytes(self, kind, progress_class, silent, capsys):
        worker = make_worker()
        normal, avoided, failed = [make_entry(kind, name) for name in ("ok", "avoided", "failed")]
        with progress_class(worker, show_progress=not silent) as display:
            add(worker, normal, avoided, failed)
            with transfer(normal, kind) as progress:
                progress.update(100)
            complete(normal, kind)
            if kind == "upload":
                avoided.status.is_instant_uploaded = True
                worker.skipped_files = 2
            else:
                with avoided.status.start_integrity_check(100) as progress:
                    progress.update(100)
                avoided.status.complete(skipped=True)
            with transfer(failed, kind) as progress:
                progress.update(25)
            complete(failed, kind)
            failed.error = OSError("late failure")
        assert display.total_transferred == 125
        assert display.failed_files == 1
        assert display.completed_files == (2 if kind == "upload" else 1)
        assert display.skipped_files == (2 if kind == "upload" else 1)
        assert display.instant_count == (1 if kind == "upload" else 0)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "\x1b" not in captured.err and "\r" not in captured.err
        if silent:
            assert captured.err == ""
        else:
            assert "1 failed" in captured.err
            assert "125 B" in captured.err
            assert "100%" not in captured.err

    @pytest.mark.parametrize("progress_class", [UploadProgress, FetchProgress])
    def test_empty_dry_run_does_not_claim_completion(self, progress_class, capsys):
        worker = make_worker()
        worker.dry_run = True
        with progress_class(worker, show_plan=True):
            worker.on_entry_added.send(worker, entries=[])
        output = capsys.readouterr()
        assert output.out == ""
        assert "plan: 0 files" in output.err
        assert "dry run, no files transferred" in output.err
        assert "finished" not in output.err and "up to date" not in output.err

    @pytest.mark.parametrize("kind,progress_class", [("upload", UploadProgress), ("fetch", FetchProgress)])
    def test_silent_dry_run_has_no_plan_or_summary(self, kind, progress_class, capsys):
        worker = make_worker()
        worker.dry_run = True
        with progress_class(worker, show_progress=False, show_plan=True) as display:
            add(worker, make_entry(kind, "planned.txt"))
        assert display.total_files == 1
        assert display.total_transferred == display.completed_files == 0
        assert capsys.readouterr() == ("", "")
