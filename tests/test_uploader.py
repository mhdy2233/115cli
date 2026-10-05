import hashlib
import threading
from unittest.mock import MagicMock, patch

import httpx
import pytest

from cli115.client import general
from cli115.uploader import Uploader, _collect_dirs, fetch_remote_tree, parse_115_export_tree
from tests.client.conftest import make_dir, make_file


def _make_client(*, parent_id=None):
    mock = MagicMock()
    mock.file.upload.return_value = make_file()
    mock.file._resolve_dir_id.side_effect = (
        FileNotFoundError("not found") if parent_id is None else None
    )
    mock.file._resolve_dir_id.return_value = parent_id
    mock.file.list.return_value = []
    return mock


class TestUploadFile:
    def test_upload_to_nonexistent_path(self, tmp_path):
        local = tmp_path / "file.txt"
        local.write_bytes(b"content")
        client = _make_client(parent_id="100")
        uploaded = make_file(name="file.txt")
        client.file.upload.return_value = uploaded

        uploader = Uploader(client)
        uploader.upload(local, "/remote/file.txt")

        assert client.file.upload.call_count == 1
        call_args = client.file.upload.call_args
        assert call_args.args[0] == "/remote/file.txt"
        assert call_args.kwargs["instant_only"] is None
        assert len(uploader.entries) == 1
        assert uploader.entries[0].remote_path == "/remote/file.txt"

    def test_upload_to_existing_directory_appends_filename(self, tmp_path):
        local = tmp_path / "file.txt"
        local.write_bytes(b"content")
        client = _make_client(parent_id="100")
        client.file.list.side_effect = [[make_dir(name="dir", id="101")], []]
        uploaded = make_file(name="file.txt")
        client.file.upload.return_value = uploaded

        uploader = Uploader(client)
        uploader.upload(local, "/remote/dir")

        assert client.file.upload.call_count == 1
        call_args = client.file.upload.call_args
        assert call_args.args[0] == "/remote/dir/file.txt"
        assert call_args.kwargs["instant_only"] is None
        assert len(uploader.entries) == 1

    def test_upload_instant_only_threshold_passed_through(self, tmp_path):
        local = tmp_path / "file.txt"
        local.write_bytes(b"content")
        client = _make_client(parent_id="100")
        threshold = 100 * 1024 * 1024  # 100 MB

        uploader = Uploader(client)
        uploader.upload(local, "/remote/file.txt", instant_only=threshold)

        assert client.file.upload.call_count == 1
        call_args = client.file.upload.call_args
        assert call_args.kwargs["instant_only"] == threshold

    def test_upload_file_part_size_passed_through(self, tmp_path):
        local = tmp_path / "file.txt"
        local.write_bytes(b"content")
        client = _make_client(parent_id="100")
        part_size = 32 * 1024 * 1024  # 32 MB

        uploader = Uploader(client, part_size=part_size)
        uploader.upload(local, "/remote/file.txt")

        assert client.file.upload.call_count == 1
        call_args = client.file.upload.call_args
        assert call_args.kwargs["part_size"] == part_size

    @pytest.mark.parametrize("destination,expected_path,listed_ids,parent_path", [
        ("/remote/target.txt", "/remote/target.txt", ["10"], "/remote"),
        ("remote\\target.txt", "/remote/target.txt", ["10"], "/remote"),
        ("/remote/dir", "/remote/dir/file.txt", ["10", "20"], "/remote"),
        ("remote\\dir\\", "/remote/dir/file.txt", ["10", "20"], "/remote"),
        ("/", "/file.txt", ["0"], None),
        ("target.txt", "/target.txt", ["0"], None),
    ])
    def test_preflight_metadata_is_reused_by_real_upload(
        self, tmp_path, destination, expected_path, listed_ids, parent_path
    ):
        local = tmp_path / "file.txt"
        local.write_bytes(b"content")
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.path == "/files/getid":
                assert request.method == "GET"
                assert request.url.params["path"] == "/remote"
                return httpx.Response(200, json={"state": True, "id": "10"})
            if request.url.path == "/files/order":
                assert request.method == "POST"
                return httpx.Response(200, json={"state": True})
            assert request.method == "GET" and request.url.path == "/files"
            items = (
                [{"cid": "20", "pid": "10", "n": "dir"}]
                if request.url.params["cid"] == "10" else []
            )
            return httpx.Response(200, json={
                "state": True, "data": items, "count": len(items),
                "offset": 0, "limit": int(request.url.params["limit"]),
            })

        auth = MagicMock()
        auth.get_cookies.return_value = {}
        client = general.Client(auth, transport=httpx.MockTransport(respond))
        with patch.object(client.file._uploader, "simple_upload", return_value={
            "data": {"file_id": "30", "file_name": expected_path.rsplit("/", 1)[1],
                     "file_size": 7, "sha1": hashlib.sha1(b"content").hexdigest()}
        }) as upload:
            result = Uploader(client).upload(local, destination)

        assert result.path == expected_path
        assert result.parent_id == listed_ids[-1]
        assert upload.call_count == 1
        assert upload.call_args.kwargs["pid"] == listed_ids[-1]
        assert [request.url.params["path"] for request in requests
                if request.url.path == "/files/getid"] == (
            [parent_path] if parent_path else []
        )
        assert [request.url.params["cid"] for request in requests
                if request.url.path == "/files"] == listed_ids
        assert sum(request.url.path == "/files/order" for request in requests) == len(listed_ids)

    @pytest.mark.parametrize("lookup,error", [
        ("_resolve_dir_id", FileNotFoundError("missing parent")),
        ("_resolve_dir_id", NotADirectoryError("parent is a file")),
        ("list", OSError("lookup failed")),
        ("list", FileNotFoundError("parent disappeared")),
    ])
    def test_parent_and_preflight_failures_abort_upload(self, tmp_path, lookup, error):
        local = tmp_path / "file.txt"
        local.write_bytes(b"content")
        client = _make_client(parent_id="100")
        getattr(client.file, lookup).side_effect = error
        uploader = Uploader(client)
        with pytest.raises(type(error), match=str(error)):
            uploader.upload(local, "/remote/file.txt")
        assert uploader.entries == []
        client.file.upload.assert_not_called()

    @pytest.mark.parametrize("by_name", [False, True])
    def test_appended_filename_cannot_replace_a_directory(self, tmp_path, by_name):
        local = tmp_path / "file.txt"
        local.write_bytes(b"content")
        client = _make_client(parent_id="0")
        client.file.list.side_effect = [
            [make_dir(name="remote", id="100")],
            [make_dir(name="file.txt", id="101")],
        ]
        with pytest.raises(FileExistsError, match="is a directory"):
            Uploader(client, dedup_by_name=by_name).upload(local, "/remote")
        client.file.upload.assert_not_called()

class TestUploadDirectory:
    def test_upload_dir_to_nonexistent_remote_creates_it(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")

        client = _make_client()
        client.file.stat.side_effect = FileNotFoundError("not found")
        dest_dir = make_dir(name=tmp_path.name)
        client.file.create_directory.return_value = dest_dir

        uploader = Uploader(client)
        uploader.upload(str(tmp_path), "/remote/newdir", no_target_dir=True)

        assert isinstance(uploader, Uploader)
        client.file.create_directory.assert_any_call("/remote/newdir", parents=True)

    def test_upload_dir_to_existing_remote_dir_creates_subdir(self, tmp_path):
        (tmp_path / "file.txt").write_text("content")

        client = _make_client()
        client.file.stat.side_effect = FileNotFoundError()
        dest_dir = make_dir(name=tmp_path.name)
        client.file.create_directory.return_value = dest_dir

        uploader = Uploader(client)
        uploader.upload(str(tmp_path), "/remote/existing")

        assert isinstance(uploader, Uploader)
        expected_dest = "/remote/existing/" + tmp_path.name
        client.file.create_directory.assert_any_call(expected_dest, parents=True)

    def test_upload_dir_to_existing_remote_subdir_merges(self, tmp_path):
        (tmp_path / "file.txt").write_text("content")

        client = _make_client()
        existing_dest_dir = make_dir(name=tmp_path.name)
        client.file.stat.return_value = existing_dest_dir

        uploader = Uploader(client)
        result = uploader.upload(str(tmp_path), "/remote/existing")

        expected_dest = "/remote/existing/" + tmp_path.name
        client.file.create_directory.assert_not_called()
        assert result.id == existing_dest_dir.id
        client.file.upload.assert_called_once_with(
            f"{expected_dest}/file.txt",
            str(tmp_path / "file.txt"),
            instant_only=None,
            part_size=None,
            check_exists=False,
            dir_id=existing_dest_dir.id,
            status=uploader.entries[0].status,
        )

    def test_upload_dir_in_memory_deduplication_skips_existing_files(self, tmp_path):
        (tmp_path / "existing.txt").write_text("existing content")
        (tmp_path / "new_file.txt").write_text("new content")

        client = _make_client()
        client.file.stat.return_value = make_dir(name="existing")
        dest_dir = make_dir(name=tmp_path.name, id="888")
        client.file.create_directory.return_value = dest_dir
        client.file._resolve_dir_id.side_effect = None
        client.file._resolve_dir_id.return_value = "888"
        client.file.list.return_value = [make_file(
            name="existing.txt", size=len(b"existing content"),
            sha1=hashlib.sha1(b"existing content").hexdigest(),
        )]

        uploader = Uploader(client)
        uploader.upload(str(tmp_path), "/remote/existing")

        # Only new_file.txt should be queued and uploaded; existing.txt is excluded from queue
        assert client.file.upload.call_count == 1
        assert client.file.upload.call_args.args[0].endswith("new_file.txt")
        assert len(uploader.entries) == 1
        assert uploader.entries[0].remote_path.endswith("new_file.txt")
        assert uploader.entries[0].error is None
        assert uploader.skipped_files == 1
    def test_upload_dir_to_remote_file_raises(self, tmp_path):
        (tmp_path / "file.txt").write_text("content")

        client = _make_client()
        client.file.stat.return_value = make_file(name="remote.txt")
        client.file._resolve_dir_id.side_effect = NotADirectoryError("not a directory")

        uploader = Uploader(client)
        with pytest.raises(NotADirectoryError):
            uploader.upload(str(tmp_path), "/remote/file.txt")

        client.file.upload.assert_not_called()

    def test_upload_dir_all_files_are_uploaded(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        (tmp_path / "c.txt").write_text("c")

        client = _make_client()
        client.file.stat.side_effect = FileNotFoundError("not found")
        client.file.create_directory.return_value = make_dir()

        uploader = Uploader(client)
        uploader.upload(str(tmp_path), "/remote/dest")

        assert client.file.upload.call_count == 3
        uploaded_names = {
            c.args[0].rsplit("/", 1)[-1] for c in client.file.upload.call_args_list
        }
        assert uploaded_names == {"a.txt", "b.txt", "c.txt"}
        assert len(uploader.entries) == 3

    def test_upload_dir_continues_and_records_file_errors(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")

        client = _make_client()
        client.file.stat.side_effect = FileNotFoundError("not found")
        client.file.create_directory.return_value = make_dir()
        client.file.upload.side_effect = [RuntimeError("network error"), make_file()]

        uploader = Uploader(client)
        uploader.upload(str(tmp_path), "/remote/dest")

        assert client.file.upload.call_count == 2
        assert len(uploader.entries) == 2
        failed_entries = [
            entry for entry in uploader.entries if entry.error is not None
        ]
        assert len(failed_entries) == 1

        failed_entry = failed_entries[0]
        assert failed_entry.remote_path.endswith("/a.txt")
        assert str(failed_entry.error) == "network error"

    def test_upload_dir_multilevel_subdirs_created(self, tmp_path):
        # Structure:
        #   tmp/
        #     root.txt
        #     sub1/
        #       mid.txt
        #       sub2/
        #         deep.txt
        (tmp_path / "root.txt").write_text("root")
        sub1 = tmp_path / "sub1"
        sub1.mkdir()
        (sub1 / "mid.txt").write_text("mid")
        sub2 = sub1 / "sub2"
        sub2.mkdir()
        (sub2 / "deep.txt").write_text("deep")

        client = _make_client()
        client.file.stat.side_effect = FileNotFoundError("not found")
        client.file.create_directory.return_value = make_dir()

        uploader = Uploader(client)
        uploader.upload(str(tmp_path), "/remote/dest")

        # Base dir + sub1 + sub1/sub2
        assert client.file.create_directory.call_count == 3
        create_paths = [c.args[0] for c in client.file.create_directory.call_args_list]
        assert f"/remote/dest/{tmp_path.name}" in create_paths
        assert any("sub1" in p for p in create_paths)
        assert any("sub2" in p for p in create_paths)

        # All 3 files uploaded
        assert client.file.upload.call_count == 3

    def test_upload_dir_instant_only_passed_to_file_uploads(self, tmp_path):
        (tmp_path / "file.txt").write_text("content")

        client = _make_client()
        client.file.stat.side_effect = FileNotFoundError("not found")
        client.file.create_directory.return_value = make_dir()
        threshold = 50 * 1024 * 1024  # 50 MB

        uploader = Uploader(client)
        uploader.upload(str(tmp_path), "/remote/dest", instant_only=threshold)

        client.file.upload.assert_called_once()
        assert client.file.upload.call_args.kwargs["instant_only"] == threshold


class TestUploadDirectoryPatterns:
    def test_exclude_pattern_filters_files(self, tmp_path):
        (tmp_path / "app.py").write_text("code")
        (tmp_path / "debug.log").write_text("log")
        (tmp_path / "error.log").write_text("log")

        client = _make_client()
        client.file.stat.side_effect = FileNotFoundError("not found")
        client.file.create_directory.return_value = make_dir()

        uploader = Uploader(client)
        uploader.upload(str(tmp_path), "/remote/dest", exclude=["**/*.log"])

        client.file.create_directory.assert_called_once_with(
            f"/remote/dest/{tmp_path.name}", parents=True
        )
        assert client.file.upload.call_count == 1
        uploaded_name = client.file.upload.call_args.args[0].rsplit("/", 1)[-1]
        assert uploaded_name == "app.py"

    def test_include_pattern_filters_files(self, tmp_path):
        (tmp_path / "main.py").write_text("code")
        (tmp_path / "utils.py").write_text("code")
        (tmp_path / "README.md").write_text("docs")

        client = _make_client()
        client.file.stat.side_effect = FileNotFoundError("not found")
        client.file.create_directory.return_value = make_dir()

        uploader = Uploader(client)
        uploader.upload(str(tmp_path), "/remote/dest", include=["**/*.py"])

        client.file.create_directory.assert_called_once_with(
            f"/remote/dest/{tmp_path.name}", parents=True
        )
        assert client.file.upload.call_count == 2
        uploaded_names = {
            c.args[0].rsplit("/", 1)[-1] for c in client.file.upload.call_args_list
        }
        assert uploaded_names == {"main.py", "utils.py"}

    def test_exclude_subdirectory_pattern(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.py").write_text("code")
        temp = tmp_path / "temp"
        temp.mkdir()
        (temp / "cache.bin").write_text("cache")

        client = _make_client()
        client.file.stat.side_effect = FileNotFoundError("not found")
        client.file.create_directory.return_value = make_dir()

        uploader = Uploader(client)
        uploader.upload(str(tmp_path), "/remote/dest", exclude=["temp/**"])

        created_dirs = [c.args[0] for c in client.file.create_directory.call_args_list]
        assert f"/remote/dest/{tmp_path.name}" in created_dirs
        assert f"/remote/dest/{tmp_path.name}/src" in created_dirs
        assert not any("temp" in p for p in created_dirs)
        assert client.file.upload.call_count == 1
        uploaded_name = client.file.upload.call_args.args[0].rsplit("/", 1)[-1]
        assert uploaded_name == "main.py"

    def test_include_and_exclude_combined(self, tmp_path):
        (tmp_path / "keep.py").write_text("code")
        (tmp_path / "skip_test.py").write_text("test code")
        (tmp_path / "data.csv").write_text("data")

        client = _make_client()
        client.file.stat.side_effect = FileNotFoundError("not found")
        client.file.create_directory.return_value = make_dir()

        uploader = Uploader(client)
        uploader.upload(
            str(tmp_path),
            "/remote/dest",
            include=["**/*.py"],
            exclude=["**/skip_*"],
        )

        client.file.create_directory.assert_called_once_with(
            f"/remote/dest/{tmp_path.name}", parents=True
        )
        assert client.file.upload.call_count == 1
        uploaded_name = client.file.upload.call_args.args[0].rsplit("/", 1)[-1]
        assert uploaded_name == "keep.py"

    def test_no_patterns_uploads_all(self, tmp_path):
        (tmp_path / "a.py").write_text("a")
        (tmp_path / "b.log").write_text("b")

        client = _make_client()
        client.file.stat.side_effect = FileNotFoundError("not found")
        client.file.create_directory.return_value = make_dir()

        uploader = Uploader(client)
        uploader.upload(str(tmp_path), "/remote/dest")

        client.file.create_directory.assert_called_once_with(
            f"/remote/dest/{tmp_path.name}", parents=True
        )
        assert client.file.upload.call_count == 2


class TestNoTargetDirectory:
    def test_dir_upload_no_target_dir_uses_remote_as_dest(self, tmp_path):
        (tmp_path / "file.txt").write_text("content")

        client = _make_client()
        client.file.stat.return_value = make_dir(name="existing")
        client.file.create_directory.return_value = make_dir()

        uploader = Uploader(client)
        uploader.upload(str(tmp_path), "/remote/existing", no_target_dir=True)

        client.file.create_directory.assert_not_called()
        assert client.file.upload.call_args.args[0] == "/remote/existing/file.txt"


class TestDryRun:
    def test_dry_run_no_upload_called(self, tmp_path):
        local = tmp_path / "file.txt"
        local.write_bytes(b"content")
        client = _make_client(parent_id="100")

        uploader = Uploader(client, dry_run=True)
        uploader.upload(local, "/remote/file.txt")

        client.file.upload.assert_not_called()
        assert len(uploader.entries) == 1

    def test_dry_run_directory_no_upload_called(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")

        client = _make_client()
        client.file.stat.side_effect = FileNotFoundError("not found")

        uploader = Uploader(client, dry_run=True)
        uploader.upload(str(tmp_path), "/remote/dest")

        client.file.upload.assert_not_called()
        client.file.create_directory.assert_not_called()
        assert len(uploader.entries) == 2

class TestUploadConcurrencyAndPartSize:
    def test_upload_dir_part_size_and_concurrency(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        (tmp_path / "c.txt").write_text("c")

        client = _make_client()
        client.file.stat.side_effect = FileNotFoundError("not found")
        client.file.create_directory.return_value = make_dir()
        part_size = 64 * 1024 * 1024  # 64 MB

        uploader = Uploader(client, part_size=part_size, max_workers=3)
        uploader.upload(str(tmp_path), "/remote/dest")

        assert client.file.upload.call_count == 3
        for call in client.file.upload.call_args_list:
            assert call.kwargs["part_size"] == part_size

    def test_uploader_defers_dir_creation_until_needed(self, tmp_path):
        sub1 = tmp_path / "sub1"
        sub1.mkdir()
        (sub1 / "old.txt").write_text("old content")
        sub2 = tmp_path / "sub2"
        sub2.mkdir()
        (sub2 / "new.txt").write_text("new content")

        client = _make_client()
        dest_dir = make_dir(name="existing", id="100", path="/remote/existing")
        sub1_dir = make_dir(name="sub1", id="101", path="/remote/existing/sub1")

        def mock_stat(p):
            if p == "/remote/existing":
                return dest_dir
            if p == "/remote/existing/sub1":
                return sub1_dir
            raise FileNotFoundError(f"not found: {p}")

        client.file.stat.side_effect = mock_stat
        client.file._resolve_dir_id.side_effect = lambda path: mock_stat(path).id
        listings = {
            "100": [sub1_dir],
            "101": [make_file(name="old.txt", size=len(b"old content"),
                              sha1=hashlib.sha1(b"old content").hexdigest())],
        }
        client.file.list.side_effect = lambda directory, **kwargs: listings[directory.id]
        client.file.create_directory.return_value = make_dir(name="sub2", id="102", path="/remote/existing/sub2")
        client.file.upload.return_value = make_file(name="new.txt")

        uploader = Uploader(client)
        uploader.upload(str(tmp_path), "/remote/existing", no_target_dir=True)

        # Only new.txt is queued
        assert len(uploader.entries) == 1
        assert uploader.entries[0].remote_path.endswith("sub2/new.txt")

        # create_directory must NOT be called for sub1 (since sub1 already existed and had no needed files)
        created_paths = [c.args[0] for c in client.file.create_directory.call_args_list]
        assert "/remote/existing/sub1" not in created_paths
        assert "/remote/existing/sub2" in created_paths


class TestUploadRegressions:
    @pytest.mark.parametrize(
        "content, destination, expected",
        [
            ("|-root\n| |-probe.txt", "/backups/root", "/backups/root/probe.txt"),
            (
                "|——根目录\n| |-root\n| | |-probe.txt\n",
                "/backups/root",
                "/backups/root/probe.txt",
            ),
            (
                "|——根目录\n| |-root\n| | |-probe.txt\n",
                "/",
                "/root/probe.txt",
            ),
            ("|——根目录\n| |-probe.txt", "/", "/probe.txt"),
        ],
    )
    def test_export_virtual_root(self, content, destination, expected):
        assert parse_115_export_tree(content, destination)[1] == {expected}

    @pytest.mark.parametrize(
        "content",
        [
            "|——根目录\n| |-other\n| | |-probe.txt",
            "|——根目录\n| |-root\n| |-other",
            "|-root\n|-other",
            "|-other\n| |-probe.txt",
        ],
    )
    def test_export_rejects_multiple_or_mismatched_roots(self, content):
        with pytest.raises(ValueError):
            parse_115_export_tree(content, "/backups/root")

    def test_virtual_root_export_preserves_existing_paths(self):
        client = _make_client()
        client.file._resolve_dir_id.side_effect = None
        client.file._resolve_dir_id.return_value = "100"
        client.file.export_dir.return_value = {
            "content": "|——根目录\n| |-root\n| | |-probe.txt\n"
        }
        assert fetch_remote_tree(client, "/backups/root")[1] == {
            "/backups/root/probe.txt"
        }

    @pytest.mark.parametrize(
        "content, filename",
        [
            ("root\n└── -notes.txt", "-notes.txt"),
            ("|-root\n| |-a|——b.txt", "a|——b.txt"),
        ],
    )
    def test_export_only_removes_tree_prefix(self, content, filename):
        assert parse_115_export_tree(content, "/root")[1] == {f"/root/{filename}"}

    def test_export_preserves_destination_and_filenames(self):
        dirs, files = parse_115_export_tree(
            "reports\n├── docs\n│   └── report(1)\n└── done.txt", "/docs/reports"
        )
        assert dirs == {"/docs/reports", "/docs/reports/docs"}
        assert files == {"/docs/reports/docs/report(1)", "/docs/reports/done.txt"}
        assert parse_115_export_tree("|-root\n| |-a|b.txt", "/root")[1] == {
            "/root/a|b.txt"
        }


    @pytest.mark.parametrize(
        "content", ["", "<html>error</html>", "root\n invalid", "|-root\n|-file.txt"]
    )
    def test_invalid_export_aborts(self, content):
        client = _make_client()
        client.file._resolve_dir_id.side_effect = None
        client.file._resolve_dir_id.return_value = "100"
        client.file.export_dir.return_value = {"content": content}
        with pytest.raises(OSError, match="Aborting upload"):
            fetch_remote_tree(client, "/remote")
        client.file.upload.assert_not_called()
        client.file.create_directory.assert_not_called()


    def test_collect_dirs_stops_at_normalized_destination(self):
        files = [("local", "/remote/dest/sub/a.txt")]
        assert _collect_dirs(files, "remote\\dest/") == {"/remote/dest/sub"}


    def test_single_file_failure_is_recorded(self, tmp_path):
        local = tmp_path / "a.txt"
        local.write_bytes(b"content")
        client = _make_client(parent_id="100")
        error = RuntimeError("network failure")
        client.file.upload.side_effect = error
        uploader = Uploader(client)
        with pytest.raises(RuntimeError, match="network failure"):
            uploader.upload(local, "/remote/a.txt")
        assert uploader.entries[0].error is error


    def test_file_no_target_directory_rejects_existing_directory(self):
        client = _make_client(parent_id="0")
        client.file.list.return_value = [make_dir(name="remote")]
        with pytest.raises(IsADirectoryError):
            Uploader(client).upload("/local/a.txt", "/remote", no_target_dir=True)
        client.file.upload.assert_not_called()


    def test_failed_upload_report_does_not_claim_success(self, tmp_path, capsys):
        from cli115.cmds.upload import UploadProgress

        (tmp_path / "a.txt").write_text("content")
        client = _make_client()
        client.file.create_directory.return_value = make_dir()
        client.file.upload.side_effect = RuntimeError("network failure")
        uploader = Uploader(client)
        with UploadProgress(uploader, show_progress=False) as progress:
            uploader.upload(tmp_path, "/remote")
        progress.report()
        output = capsys.readouterr().err
        assert "0 / 1 files, 1 failed" in output
        assert "100%" not in output


class TestUploadDeduplication:
    @pytest.mark.parametrize("directory", [False, True])
    def test_single_file_skips_only_identical_content(self, tmp_path, directory):
        local = tmp_path / "a.txt"
        local.write_bytes(b"content")
        remote = make_file(name="a.txt", size=7,
                           sha1=hashlib.sha1(b"content").hexdigest())
        client = _make_client(parent_id="100")
        client.file.list.side_effect = (
            [[make_dir(name="remote")], [remote]] if directory else [[remote]]
        )
        uploader = Uploader(client)
        added = []
        uploader.on_entry_added.connect(lambda sender, entries: added.append(entries), weak=False)

        assert uploader.upload(local, "/remote" if directory else "/remote/a.txt") is remote
        assert uploader.skipped_files == 1
        assert uploader.entries == []
        assert added == [[]]
        client.file.upload.assert_not_called()

    @pytest.mark.parametrize("single", [False, True])
    @pytest.mark.parametrize("existing", [
        make_file(name="a.txt", size=3, sha1=hashlib.sha1(b"old").hexdigest()),
        make_file(name="a.txt", size=2, sha1=hashlib.sha1(b"new").hexdigest()),
        make_file(name="a.txt", size=3, sha1=""),
        make_dir(name="a.txt"),
    ])
    def test_conflicts_abort_before_any_upload(self, tmp_path, single, existing):
        local = tmp_path / "a.txt"
        local.write_bytes(b"new")
        client = _make_client()
        client.file._resolve_dir_id.side_effect = None
        client.file._resolve_dir_id.return_value = "100"
        client.file.list.return_value = [existing]
        client.file.stat.return_value = existing
        uploader = Uploader(client, max_workers=3)

        with pytest.raises((FileExistsError, IsADirectoryError)):
            uploader.upload(local if single else tmp_path,
                            "/remote/a.txt" if single else "/remote",
                            no_target_dir=True)
        assert uploader.skipped_files == 0
        client.file.upload.assert_not_called()
        client.file.create_directory.assert_not_called()

    def test_remote_names_are_case_sensitive(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"new")
        client = _make_client()
        client.file._resolve_dir_id.side_effect = None
        client.file._resolve_dir_id.return_value = "100"
        client.file.list.return_value = [make_file(name="A.txt", size=3, sha1="other")]
        uploader = Uploader(client, max_workers=3)
        uploader.upload(tmp_path, "/remote", no_target_dir=True)
        assert uploader.skipped_files == 0
        client.file.upload.assert_called_once()
        assert client.file.upload.call_args.args[0] == "/remote/a.txt"

    def test_remote_file_cannot_be_parent_directory(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.txt").write_bytes(b"new")
        client = _make_client()
        client.file._resolve_dir_id.side_effect = None
        client.file._resolve_dir_id.return_value = "100"
        client.file.list.return_value = [make_file(name="sub")]
        with pytest.raises(NotADirectoryError, match="/remote/sub"):
            Uploader(client, max_workers=3).upload(tmp_path, "/remote", no_target_dir=True)
        client.file.create_directory.assert_not_called()
        client.file.upload.assert_not_called()


    def test_colliding_local_targets_abort_before_workers(self, tmp_path):
        files = [("first", "/remote/a.txt"), ("second", "/remote/a.txt ")]
        client = _make_client()
        with patch("cli115.uploader._collect_files", return_value=files):
            with pytest.raises(FileExistsError, match="multiple local files"):
                Uploader(client, max_workers=3).upload(tmp_path, "/remote", no_target_dir=True)
        client.file.create_directory.assert_not_called()
        client.file.upload.assert_not_called()

    def test_duplicate_remote_names_abort_before_workers(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"new")
        client = _make_client()
        client.file._resolve_dir_id.side_effect = None
        client.file._resolve_dir_id.return_value = "100"
        client.file.list.return_value = [make_file(name="a.txt", id="1"),
                                         make_file(name="a.txt", id="2")]
        with pytest.raises(FileExistsError, match="multiple remote entries"):
            Uploader(client, max_workers=3).upload(tmp_path, "/remote", no_target_dir=True)
        client.file.upload.assert_not_called()

    def test_unrelated_duplicate_remote_names_do_not_block_upload(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"new")
        client = _make_client()
        client.file._resolve_dir_id.side_effect = None
        client.file._resolve_dir_id.return_value = "100"
        client.file.list.return_value = [make_dir(name="unrelated", id="1"),
                                         make_dir(name="unrelated", id="2")]
        uploader = Uploader(client)
        uploader.upload(tmp_path, "/remote", no_target_dir=True)
        client.file.upload.assert_called_once()
        assert client.file.list.call_count == 1
        assert uploader.entries[0].error is None

    def test_repeated_remote_id_across_pages_is_only_counted_once(self, tmp_path):
        from cli115.client.lazy import LazyCollection
        from cli115.client.models import Pagination

        (tmp_path / "a.txt").write_bytes(b"new")
        client = _make_client()
        client.file._resolve_dir_id.side_effect = None
        client.file._resolve_dir_id.return_value = "100"
        pages = []

        def fetch(page, page_size):
            pages.append(page)
            entry = make_file(name="a.txt", id="200", size=3,
                              sha1=hashlib.sha1(b"new").hexdigest())
            return [entry], Pagination(total=2, limit=1, offset=page - 1)

        client.file.list.return_value = LazyCollection(fetch, page_size=1)
        uploader = Uploader(client)
        uploader.upload(tmp_path, "/remote", no_target_dir=True)
        assert pages == [1, 2]
        assert uploader.skipped_files == 1
        assert uploader.entries == []
        client.file.upload.assert_not_called()

    def test_paginated_listing_only_visits_relevant_directories(self, tmp_path):
        from cli115.client.lazy import LazyCollection
        from cli115.client.models import Pagination

        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.txt").write_bytes(b"new")
        client = _make_client()
        client.file._resolve_dir_id.side_effect = None
        client.file._resolve_dir_id.return_value = "100"
        root_items = [make_dir(name="irrelevant", id="200"), make_dir(name="sub", id="101")]
        pages = []

        def fetch(page, page_size):
            pages.append(page)
            return root_items[page - 1:page], Pagination(total=2, limit=1, offset=page - 1)

        def listing(directory, **kwargs):
            if directory.id == "100":
                return LazyCollection(fetch, page_size=1)
            assert directory.id == "101"
            return [make_file(name="a.txt", size=3, sha1=hashlib.sha1(b"new").hexdigest())]

        client.file.list.side_effect = listing
        uploader = Uploader(client, max_workers=3)
        uploader.upload(tmp_path, "/remote", no_target_dir=True)
        assert pages == [1, 2]
        assert client.file.list.call_count == 2
        assert uploader.skipped_files == 1
        client.file.upload.assert_not_called()
        client.file.export_dir.assert_not_called()

    @pytest.mark.parametrize("destination", ["/remote", "/"])
    def test_workers_use_unique_targets_and_cached_directory_ids(self, tmp_path, destination):
        for name in ("a.txt", "b.txt", "c.txt"):
            (tmp_path / name).write_text(name)
        client = _make_client()
        client.file._resolve_dir_id.side_effect = None
        client.file._resolve_dir_id.return_value = "100"
        barrier = threading.Barrier(3, timeout=5)
        seen = []
        lock = threading.Lock()

        def upload(path, local, **kwargs):
            with lock:
                seen.append((path, kwargs["dir_id"]))
            barrier.wait()
            return make_file()

        client.file.upload.side_effect = upload
        uploader = Uploader(client, max_workers=3)
        uploader.upload(tmp_path, destination, no_target_dir=True)
        assert len(seen) == len(set(seen)) == 3
        assert {directory_id for _, directory_id in seen} == {"100"}
        assert all(entry.error is None for entry in uploader.entries)
        assert client.file.list.call_count == 1
        client.file.create_directory.assert_not_called()

    def test_missing_directory_id_does_not_allow_same_named_file(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"new")
        client = _make_client()
        client.file.stat.return_value = make_file(name="remote")
        with pytest.raises(NotADirectoryError, match="/remote"):
            Uploader(client, max_workers=3).upload(tmp_path, "/remote", no_target_dir=True)
        client.file.create_directory.assert_not_called()
        client.file.upload.assert_not_called()


class TestUploadDeduplicationByName:
    @pytest.mark.parametrize("dry_run", [False, True])
    def test_exported_names_skip_without_listing_or_hashing(self, tmp_path, dry_run):
        (tmp_path / "a.txt").write_bytes(b"different local content")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_bytes(b"b")
        client = _make_client()
        client.file._resolve_dir_id.side_effect = None
        client.file._resolve_dir_id.return_value = "100"
        client.file.export_dir.return_value = {
            "content": "|-remote\n| |-a.txt\n| |-sub\n| | |-b.txt"
        }
        uploader = Uploader(client, dedup_by_name=True, dry_run=dry_run, max_workers=3)
        added = []
        uploader.on_entry_added.connect(lambda sender, entries: added.append(entries), weak=False)

        with patch("cli115.uploader.sha1_file", side_effect=AssertionError("must not hash")):
            uploader.upload(tmp_path, "/remote", no_target_dir=True)

        assert uploader.skipped_files == 2
        assert uploader.entries == []
        assert added == [[]]
        client.file.export_dir.assert_called_once()
        client.file._resolve_dir_id.assert_called_once_with("/remote")
        client.file.list.assert_not_called()
        client.file.stat.assert_not_called()
        client.file.create_directory.assert_not_called()
        client.file.upload.assert_not_called()

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_relative_paths_and_only_needed_parent_ids_for_workers(self, tmp_path, dry_run):
        for directory in ("old", "other"):
            (tmp_path / directory).mkdir()
            (tmp_path / directory / "a.txt").write_bytes(b"a")
        (tmp_path / "other" / "b.txt").write_bytes(b"b")
        client = _make_client()
        ids = {"/remote": "100", "/remote/other": "102"}
        client.file._resolve_dir_id.side_effect = ids.__getitem__
        client.file.export_dir.return_value = {
            "content": "|-remote\n| |-old\n| | |-a.txt\n| |-other\n| | |-seed.txt"
        }
        barrier = threading.Barrier(2, timeout=5)

        def upload(path, local, **kwargs):
            assert kwargs["dir_id"] == "102"
            barrier.wait()
            return make_file()

        client.file.upload.side_effect = upload
        uploader = Uploader(client, dedup_by_name=True, dry_run=dry_run, max_workers=2)
        with patch("cli115.uploader.sha1_file", side_effect=AssertionError("must not hash")):
            uploader.upload(tmp_path, "remote\\", no_target_dir=True)

        assert uploader.skipped_files == 1
        assert {entry.remote_path for entry in uploader.entries} == {
            "/remote/other/a.txt", "/remote/other/b.txt"
        }
        assert all(entry.error is None for entry in uploader.entries)
        assert client.file.upload.call_count == (0 if dry_run else 2)
        assert client.file._resolve_dir_id.call_count == (1 if dry_run else 2)
        client.file.export_dir.assert_called_once()
        client.file.list.assert_not_called()
        client.file.stat.assert_not_called()
        client.file.create_directory.assert_not_called()

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_single_file_skips_different_size_and_content(self, tmp_path, dry_run):
        local = tmp_path / "a.txt"
        local.write_bytes(b"new")
        client = _make_client(parent_id="100")
        existing = make_file(name="a.txt", size=999, sha1="different")
        client.file.list.return_value = [existing]
        uploader = Uploader(client, dedup_by_name=True, dry_run=dry_run)
        with patch("cli115.uploader.sha1_file", side_effect=AssertionError("must not hash")):
            result = uploader.upload(local, "/remote/a.txt")
        assert result is (None if dry_run else existing)
        assert uploader.skipped_files == 1
        assert uploader.entries == []
        client.file._resolve_dir_id.assert_called_once_with("/remote")
        client.file.list.assert_called_once()
        client.file.stat.assert_not_called()
        client.file.export_dir.assert_not_called()
        client.file.upload.assert_not_called()

    def test_single_missing_source_cannot_be_skipped(self, tmp_path):
        client = _make_client()
        client.file.stat.return_value = make_file(name="a.txt")
        uploader = Uploader(client, dedup_by_name=True)
        with pytest.raises(FileNotFoundError, match="local path"):
            uploader.upload(tmp_path / "a.txt", "/remote/a.txt")
        assert uploader.skipped_files == 0
        client.file.upload.assert_not_called()

    @pytest.mark.parametrize("content", ["", "<html>error</html>", "|-other\n| |-a.txt"])
    def test_invalid_export_never_falls_back_to_upload(self, tmp_path, content):
        (tmp_path / "a.txt").write_bytes(b"a")
        client = _make_client()
        client.file._resolve_dir_id.side_effect = None
        client.file._resolve_dir_id.return_value = "100"
        client.file.export_dir.return_value = {"content": content}
        with pytest.raises(OSError, match="Aborting upload"):
            Uploader(client, dedup_by_name=True).upload(tmp_path, "/remote", no_target_dir=True)
        client.file.export_dir.assert_called_once()
        client.file.list.assert_not_called()
        client.file.create_directory.assert_not_called()
        client.file.upload.assert_not_called()

    def test_known_directory_still_conflicts_with_local_file(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"a")
        client = _make_client()
        client.file._resolve_dir_id.side_effect = None
        client.file._resolve_dir_id.return_value = "100"
        client.file.export_dir.return_value = {
            "content": "|-remote\n| |-a.txt\n| | |-child.txt"
        }
        with pytest.raises(FileExistsError, match="is a directory"):
            Uploader(client, dedup_by_name=True).upload(tmp_path, "/remote", no_target_dir=True)
        client.file.upload.assert_not_called()
        client.file.list.assert_not_called()

    @pytest.mark.parametrize("is_directory", [False, True])
    def test_exported_leaf_is_resolved_before_using_as_parent(self, tmp_path, is_directory):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.txt").write_bytes(b"a")
        client = _make_client()
        client.file._resolve_dir_id.side_effect = [
            "100", "101" if is_directory else FileNotFoundError("not a directory")
        ]
        client.file.export_dir.return_value = {"content": "|-remote\n| |-sub"}
        uploader = Uploader(client, dedup_by_name=True)

        if is_directory:
            uploader.upload(tmp_path, "/remote", no_target_dir=True)
            client.file.upload.assert_called_once()
            assert client.file.upload.call_args.kwargs["dir_id"] == "101"
        else:
            with pytest.raises(NotADirectoryError, match="/remote/sub"):
                uploader.upload(tmp_path, "/remote", no_target_dir=True)
            client.file.upload.assert_not_called()

        assert [call.args[0] for call in client.file._resolve_dir_id.call_args_list] == [
            "/remote", "/remote/sub"
        ]
        client.file.create_directory.assert_not_called()
        client.file.list.assert_not_called()
        client.file.stat.assert_not_called()
