import hashlib
import os
from threading import Barrier, get_ident
from unittest.mock import MagicMock

import pytest

from cli115.fetcher import Fetcher, local_filename
from tests.client.conftest import make_dir, make_file


def _sha1(content: bytes) -> str:
    return hashlib.sha1(content).hexdigest().upper()


def _make_remote_file(chunks: list[bytes]) -> MagicMock:
    remote = MagicMock()
    remote.__enter__.return_value = remote
    remote.__exit__.return_value = False
    remote.read.side_effect = [*chunks, b""]
    return remote


def _make_directory_client(
    entries_by_parent: dict[str, list],
    chunks_by_file_id: dict[str, list[bytes]] | None = None,
    *,
    failing_ids: set[str] | None = None,
) -> MagicMock:
    client = MagicMock()

    def list_side_effect(directory, **kwargs):
        return entries_by_parent.get(directory.id, [])

    client.file.list.side_effect = list_side_effect

    failing_ids = failing_ids or set()
    chunks_by_file_id = chunks_by_file_id or {}

    def open_side_effect(entry, user_agent=None):
        if entry.id in failing_ids:
            raise RuntimeError("download failed")
        return _make_remote_file(chunks_by_file_id[entry.id])

    client.file.open.side_effect = open_side_effect
    return client


class TestFetchFile:
    @pytest.mark.parametrize("dedup_by_name", [False, True])
    def test_fetch_file_downloads_to_target_path(self, tmp_path, dedup_by_name):
        content = b"hello fetcher"
        remote_file = make_file(
            name="remote.bin",
            path="/remote/remote.bin",
            size=len(content),
            sha1=_sha1(content),
        )

        client = MagicMock()
        client.file.open.return_value = _make_remote_file([content])

        fetcher = Fetcher(client, dedup_by_name=dedup_by_name)
        output = tmp_path / "out.bin"
        result = fetcher.fetch(remote_file, str(output), check_integrity=True)

        assert result == str(output.resolve())
        assert output.read_bytes() == content
        client.file.open.assert_called_once_with(remote_file, user_agent=None)
        assert len(fetcher.entries) == 1

    @pytest.mark.parametrize("dedup_by_name", [False, True])
    def test_fetch_file_integrity_error_removes_partial_file(self, tmp_path, dedup_by_name):
        content = b"broken"
        remote_file = make_file(
            name="broken.bin",
            path="/remote/broken.bin",
            size=len(content),
            sha1="BADSHA1",
        )

        client = MagicMock()
        client.file.open.return_value = _make_remote_file([content])

        fetcher = Fetcher(client, dedup_by_name=dedup_by_name)
        output = tmp_path / "broken.bin"

        with pytest.raises(ValueError, match="sha1 mismatch"):
            fetcher.fetch(remote_file, str(output), check_integrity=True)

        assert not output.exists()
        assert not fetcher.entries[0].status.is_completed
        assert fetcher.entries[0].error is not None

    @pytest.mark.parametrize("failure", ["open", "read", "size", "sha1"])
    def test_failed_download_preserves_existing_target(self, tmp_path, failure):
        output = tmp_path / "existing.bin"
        output.write_bytes(b"original")
        client = MagicMock()
        remote = _make_remote_file([b"new"])
        client.file.open.return_value = remote
        if failure == "open":
            client.file.open.side_effect = OSError("open failed")
        elif failure == "read":
            remote.read.side_effect = [b"new", OSError("read failed")]
        entry = make_file(size=4 if failure == "size" else 3, sha1="incorrect")
        fetcher = Fetcher(client)
        with pytest.raises((OSError, ValueError)):
            fetcher.fetch(entry, output, check_integrity=failure == "sha1")
        assert output.read_bytes() == b"original"
        assert list(tmp_path.iterdir()) == [output]
        assert not fetcher.entries[0].status.is_completed

    def test_success_replaces_existing_target_after_verification(self, tmp_path):
        output = tmp_path / "existing.bin"
        output.write_bytes(b"original")
        client = MagicMock()
        client.file.open.return_value = _make_remote_file([b"new"])
        fetcher = Fetcher(client)
        entry = make_file(size=3, sha1=_sha1(b"new").lower())
        fetcher.fetch(entry, output, check_integrity=True)
        assert output.read_bytes() == b"new"
        assert fetcher.entries[0].status.is_completed
        assert list(tmp_path.iterdir()) == [output]

    @pytest.mark.parametrize("content", [b"already downloaded", b""])
    def test_identical_existing_file_is_skipped(self, tmp_path, content):
        output = tmp_path / "existing.bin"
        output.write_bytes(content)
        client = MagicMock()
        fetcher = Fetcher(client)
        entry = make_file(size=len(content), sha1=_sha1(content).lower())
        completed = []

        def on_added(sender, entries):
            entries[0].status.on_complete.connect(
                lambda status: completed.append(status.is_skipped), weak=False
            )

        fetcher.on_entry_added.connect(on_added)
        fetcher.fetch(entry, output)

        client.file.open.assert_not_called()
        assert completed == [True]
        assert fetcher.entries[0].status.is_completed
        assert output.read_bytes() == content
        assert list(tmp_path.iterdir()) == [output]

    @pytest.mark.parametrize("remote_sha1", [_sha1(b"new"), ""])
    def test_same_size_with_different_or_missing_hash_is_downloaded(
        self, tmp_path, remote_sha1
    ):
        output = tmp_path / "existing.bin"
        output.write_bytes(b"old")
        client = MagicMock()
        client.file.open.return_value = _make_remote_file([b"new"])
        fetcher = Fetcher(client)
        fetcher.fetch(make_file(size=3, sha1=remote_sha1), output)
        client.file.open.assert_called_once()
        assert output.read_bytes() == b"new"
        assert not fetcher.entries[0].status.is_skipped

    @pytest.mark.parametrize("dry_run", [False, True])
    @pytest.mark.parametrize("remote_size", [3, 1024])
    def test_name_dedup_skips_existing_file_without_reading(
        self, tmp_path, monkeypatch, dry_run, remote_size
    ):
        output = tmp_path / "existing.bin"
        output.write_bytes(b"old")
        client = MagicMock()
        fetcher = Fetcher(client, dedup_by_name=True, dry_run=dry_run)
        completed, messages = [], []

        def on_added(sender, entries):
            entries[0].status.on_complete.connect(
                lambda status: completed.append(status.is_skipped), weak=False
            )
            entries[0].status.on_message.connect(
                lambda status, message: messages.append(message), weak=False
            )

        fetcher.on_entry_added.connect(on_added)
        read = MagicMock(side_effect=AssertionError("existing file must not be read"))
        monkeypatch.setattr("cli115.fetcher.sha1_file", read)
        monkeypatch.setattr("cli115.fetcher.os.path.getsize", read)
        monkeypatch.setattr("cli115.fetcher.open", read, raising=False)
        fetcher.fetch(make_file(size=remote_size, sha1=""), output, check_integrity=True)

        read.assert_not_called()
        assert client.mock_calls == []
        assert completed == [True]
        assert messages == ["already exists (filename match), skipped"]
        assert fetcher.entries[0].status.is_completed
        assert output.read_bytes() == b"old"

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_name_dedup_rejects_directory_target(self, tmp_path, dry_run):
        client = MagicMock()
        fetcher = Fetcher(client, dedup_by_name=True, dry_run=dry_run)
        with pytest.raises(IsADirectoryError):
            fetcher.fetch(make_file(), tmp_path)
        client.file.open.assert_not_called()
        assert not fetcher.entries[0].status.is_skipped

    @pytest.mark.skipif(os.name != "nt", reason="Windows case insensitive paths")
    def test_name_dedup_uses_local_filesystem_case_matching(self, tmp_path):
        (tmp_path / "Same.txt").write_bytes(b"old")
        client = MagicMock()
        fetcher = Fetcher(client, dedup_by_name=True)
        fetcher.fetch(make_file(name="same.txt"), tmp_path / "same.txt")
        client.file.open.assert_not_called()
        assert fetcher.entries[0].status.is_skipped

    @pytest.mark.parametrize("chunk_size", [0, -1])
    def test_rejects_nonpositive_chunk_size(self, chunk_size):
        with pytest.raises(ValueError, match="chunk size"):
            Fetcher(MagicMock(), chunk_size=chunk_size)

    @pytest.mark.parametrize("max_workers", [0, -1])
    def test_rejects_nonpositive_max_workers(self, max_workers):
        with pytest.raises(ValueError, match="max workers"):
            Fetcher(MagicMock(), max_workers=max_workers)


class TestFetchDirectory:
    def test_repeated_listing_entries_are_downloaded_once(self, tmp_path):
        root = make_dir(id="10")
        subdir = make_dir(name="sub", id="11", parent_id=root.id)
        remote_file = make_file(id="12", size=3, sha1=_sha1(b"new"))
        client = _make_directory_client(
            {root.id: [subdir, subdir], subdir.id: [remote_file, remote_file]},
            {remote_file.id: [b"new"]},
        )
        fetcher = Fetcher(client)
        fetcher.fetch(root, tmp_path)
        assert len(fetcher.entries) == 1
        assert client.file.list.call_count == 2
        client.file.open.assert_called_once()

    @pytest.mark.parametrize("max_workers", [1, 3])
    @pytest.mark.parametrize("directory_collision", [False, True])
    @pytest.mark.parametrize("dedup_by_name", [False, True])
    def test_conflicting_targets_fail_before_downloading(
        self, tmp_path, directory_collision, max_workers, dedup_by_name
    ):
        root = make_dir(id="10")
        first = make_file(name="same", id="11")
        second = (make_dir if directory_collision else make_file)(name="same", id="12")
        client = _make_directory_client({root.id: [first, second]})
        with pytest.raises(FileExistsError, match="multiple remote entries"):
            Fetcher(client, max_workers=max_workers, dedup_by_name=dedup_by_name).fetch(
                root, tmp_path
            )
        client.file.open.assert_not_called()
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.skipif(os.name != "nt", reason="Windows case insensitive paths")
    def test_windows_case_collisions_fail_before_downloading(self, tmp_path):
        root = make_dir()
        client = _make_directory_client(
            {root.id: [make_file(name="Same.txt", id="11"),
                       make_file(name="same.txt", id="12")]}
        )
        with pytest.raises(FileExistsError, match="multiple remote entries"):
            Fetcher(client).fetch(root, tmp_path)
        client.file.open.assert_not_called()

    def test_existing_identical_directory_file_is_skipped(self, tmp_path):
        output = tmp_path / "same.txt"
        output.write_bytes(b"same")
        root = make_dir()
        remote_file = make_file(name=output.name, size=4, sha1=_sha1(b"same"))
        client = _make_directory_client({root.id: [remote_file]})
        fetcher = Fetcher(client)
        fetcher.fetch(root, tmp_path)
        client.file.open.assert_not_called()
        assert fetcher.entries[0].status.is_skipped
        assert fetcher.entries[0].status.is_completed

    @pytest.mark.parametrize("dry_run", [False, True])
    @pytest.mark.parametrize("max_workers", [1, 3])
    def test_name_dedup_only_skips_existing_relative_target(
        self, tmp_path, monkeypatch, dry_run, max_workers
    ):
        output = tmp_path / "same.txt"
        output.write_bytes(b"old content")
        root = make_dir(id="10")
        subdir = make_dir(name="sub", id="11")
        existing = make_file(name=output.name, id="12", size=3, sha1="")
        missing = make_file(name=output.name, id="13", size=3, sha1=_sha1(b"new"))
        client = _make_directory_client(
            {root.id: [existing, subdir], subdir.id: [missing]},
            {missing.id: [b"new"]},
        )
        read = MagicMock(side_effect=AssertionError("existing file must not be read"))
        monkeypatch.setattr("cli115.fetcher.sha1_file", read)
        monkeypatch.setattr("cli115.fetcher.os.path.getsize", read)
        monkeypatch.setattr("cli115.fetcher.open", read, raising=False)
        fetcher = Fetcher(
            client, dedup_by_name=True, dry_run=dry_run, max_workers=max_workers
        )
        fetcher.fetch(root, tmp_path)

        read.assert_not_called()
        assert client.file.list.call_count == 2
        assert [entry.status.is_skipped for entry in fetcher.entries] == [True, False]
        assert all(entry.error is None for entry in fetcher.entries)
        assert output.read_bytes() == b"old content"
        if dry_run:
            client.file.open.assert_not_called()
            assert not (tmp_path / "sub").exists()
            assert not fetcher.entries[1].status.is_completed
        else:
            client.file.open.assert_called_once_with(missing, user_agent=None)
            assert (tmp_path / "sub" / "same.txt").read_bytes() == b"new"
            assert fetcher.entries[1].status.is_completed

    def test_concurrent_downloads_deduplicate_and_isolate_failures(self, tmp_path):
        root = make_dir()
        contents = {"1": b"one", "2": b"two", "3": b"bad", "4": b"same"}
        files = {
            id: make_file(id=id, name=f"{id}.txt", size=len(content), sha1=_sha1(content))
            for id, content in contents.items()
        }
        client = _make_directory_client(
            {root.id: [files[id] for id in ["1", "1", "4", "3", "2"]]}
        )
        barrier = Barrier(2, timeout=3)
        threads = []

        def open_file(entry, user_agent=None):
            if entry.id == "3":
                raise OSError("download failed")
            chunks = iter([contents[entry.id], b""])

            def read(size):
                chunk = next(chunks)
                if chunk:
                    threads.append(get_ident())
                    barrier.wait()
                return chunk

            remote = _make_remote_file([])
            remote.read.side_effect = read
            return remote

        client.file.open.side_effect = open_file
        (tmp_path / "3.txt").write_bytes(b"old")
        (tmp_path / "4.txt").write_bytes(contents["4"])
        fetcher = Fetcher(client, max_workers=2)
        fetcher.fetch(root, tmp_path, check_integrity=True)

        entries = {entry.remote_entry.id: entry for entry in fetcher.entries}
        assert len(entries) == len(fetcher.entries) == 4
        assert len(set(threads)) == 2
        assert sorted(call.args[0].id for call in client.file.open.call_args_list) == ["1", "2", "3"]
        assert entries["4"].status.is_skipped
        assert str(entries["3"].error) == "download failed"
        assert not entries["3"].status.is_completed
        for id in ["1", "2", "4"]:
            assert entries[id].error is None
            assert entries[id].status.is_completed
            assert (tmp_path / f"{id}.txt").read_bytes() == contents[id]
        assert (tmp_path / "3.txt").read_bytes() == b"old"
        assert not list(tmp_path.glob("*.part"))

    @pytest.mark.parametrize("name", ["../outside.txt", "sub/../../outside", "..\\outside", "/absolute", ".."])
    def test_rejects_remote_path_traversal(self, tmp_path, name):
        root = make_dir()
        client = _make_directory_client({root.id: [make_file(name=name)]})
        with pytest.raises(ValueError, match="invalid local filename"):
            Fetcher(client).fetch(root, tmp_path / "out")
        client.file.open.assert_not_called()

    @pytest.mark.skipif(os.name != "nt", reason="Windows filename rules")
    @pytest.mark.parametrize("name", ["C:outside", "NUL.txt", "file:stream", "trailing."])
    def test_rejects_reserved_windows_names(self, name):
        with pytest.raises(ValueError, match="invalid local filename"):
            local_filename(name)

    def test_fetch_directory_downloads_nested_structure(self, tmp_path):
        root = make_dir(name="root", id="50", path="/remote/root")
        docs = make_dir(name="docs", id="51", parent_id="50", path="/remote/root/docs")
        images = make_dir(
            name="images",
            id="52",
            parent_id="50",
            path="/remote/root/images",
        )
        icons = make_dir(
            name="icons",
            id="53",
            parent_id="52",
            path="/remote/root/images/icons",
        )

        root_file_content = b"root file"
        doc_file_content = b"doc file"
        image_file_content = b"image bytes"
        icon_file_content = b"icon bytes"

        root_file = make_file(
            name="README.md",
            id="60",
            parent_id="50",
            path="/remote/root/README.md",
            size=len(root_file_content),
            sha1=_sha1(root_file_content),
        )
        doc_file = make_file(
            name="guide.txt",
            id="61",
            parent_id="51",
            path="/remote/root/docs/guide.txt",
            size=len(doc_file_content),
            sha1=_sha1(doc_file_content),
        )
        image_file = make_file(
            name="logo.png",
            id="62",
            parent_id="52",
            path="/remote/root/images/logo.png",
            size=len(image_file_content),
            sha1=_sha1(image_file_content),
        )
        icon_file = make_file(
            name="app.ico",
            id="63",
            parent_id="53",
            path="/remote/root/images/icons/app.ico",
            size=len(icon_file_content),
            sha1=_sha1(icon_file_content),
        )

        client = _make_directory_client(
            {
                root.id: [docs, images, root_file],
                docs.id: [doc_file],
                images.id: [icons, image_file],
                icons.id: [icon_file],
            },
            {
                root_file.id: [root_file_content],
                doc_file.id: [doc_file_content],
                image_file.id: [image_file_content],
                icon_file.id: [icon_file_content],
            },
        )

        output_dir = tmp_path / "downloads"

        fetcher = Fetcher(client)
        fetcher.fetch(root, str(output_dir))

        expected_root = output_dir
        assert (expected_root / "README.md").read_bytes() == root_file_content
        assert (expected_root / "docs").is_dir()
        assert (expected_root / "docs" / "guide.txt").read_bytes() == doc_file_content
        assert (expected_root / "images").is_dir()
        assert (
            expected_root / "images" / "logo.png"
        ).read_bytes() == image_file_content
        assert (expected_root / "images" / "icons").is_dir()
        assert (
            expected_root / "images" / "icons" / "app.ico"
        ).read_bytes() == icon_file_content
        assert client.file.open.call_count == 4
        assert len(fetcher.entries) == 4

    def test_fetch_directory_include_and_exclude(self, tmp_path):
        root = make_dir(name="root", id="10", path="/remote/root")
        sub = make_dir(name="sub", id="11", parent_id="10", path="/remote/root/sub")

        keep_content = b"keep"
        skip_content = b"skip"
        nested_content = b"nest"

        keep_file = make_file(
            name="keep.txt",
            id="20",
            parent_id="10",
            path="/remote/root/keep.txt",
            size=len(keep_content),
            sha1=_sha1(keep_content),
        )
        skip_file = make_file(
            name="skip.log",
            id="21",
            parent_id="10",
            path="/remote/root/skip.log",
            size=len(skip_content),
            sha1=_sha1(skip_content),
        )
        nested_file = make_file(
            name="nested.txt",
            id="22",
            parent_id="11",
            path="/remote/root/sub/nested.txt",
            size=len(nested_content),
            sha1=_sha1(nested_content),
        )

        client = _make_directory_client(
            {
                root.id: [sub, keep_file, skip_file],
                sub.id: [nested_file],
            },
            {
                keep_file.id: [keep_content],
                skip_file.id: [skip_content],
                nested_file.id: [nested_content],
            },
        )

        output_dir = tmp_path / "downloads"

        fetcher = Fetcher(client)
        fetcher.fetch(
            root,
            str(output_dir),
            include=["**/*.txt"],
            exclude=["sub/**"],
        )

        expected_root = output_dir
        assert (expected_root / "keep.txt").exists()
        assert not (expected_root / "skip.log").exists()
        assert not (expected_root / "sub" / "nested.txt").exists()
        assert client.file.open.call_count == 1
        assert len(fetcher.entries) == 1

    def test_fetch_directory_error(self, tmp_path):
        root = make_dir(name="root", id="40", path="/remote/root")
        bad_file = make_file(
            name="bad.txt",
            id="41",
            parent_id="40",
            path="/remote/root/bad.txt",
            size=3,
            sha1=_sha1(b"bad"),
        )
        ok_file = make_file(
            name="ok.txt",
            id="42",
            parent_id="40",
            path="/remote/root/ok.txt",
            size=2,
            sha1=_sha1(b"ok"),
        )

        client = _make_directory_client(
            {root.id: [bad_file, ok_file]},
            {ok_file.id: [b"ok"]},
            failing_ids={bad_file.id},
        )

        output_dir = tmp_path / "out"

        fetcher = Fetcher(client)
        fetcher.fetch(root, str(output_dir))

        by_id = {entry.remote_entry.id: entry for entry in fetcher.entries}
        assert str(by_id[bad_file.id].error) == "download failed"
        assert by_id[ok_file.id].error is None
        assert (output_dir / "ok.txt").exists()
