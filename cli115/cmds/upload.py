"""Upload command."""

from __future__ import annotations

import argparse
import logging
import os
from cli115.cmds.base import BaseCommand, WorkerCommand
from cli115.cmds.formatter import PairFormatterMixin, format_entry
from cli115.helpers import parse_size
from cli115.exceptions import CommandLineError
from cli115.uploader import Uploader
from cli115.cmds.progress import TransferProgress


class UploadCommand(PairFormatterMixin, WorkerCommand, BaseCommand):
    """Upload a local file or directory to the remote path."""

    uploader: Uploader | None = None

    def register(self, parser: argparse.ArgumentParser) -> None:
        super().register(parser)
        parser.add_argument("local_path", help="local file or directory path")
        parser.add_argument("remote_path", help="remote destination path")
        parser.add_argument(
            "--dedup-by-name",
            action="store_true",
            help=(
                "Skip existing destination paths by name only; directory uploads "
                "use one exported tree without comparing size or SHA-1"
            ),
        )
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
        if max_workers is None:
            max_workers = 1
        if max_workers < 1:
            raise CommandLineError("max workers must be greater than zero")

        self.uploader = Uploader(
            self._create_client(),
            dry_run=args.dry_run,
            part_size=part_size,
            max_workers=max_workers,
            dedup_by_name=args.dedup_by_name,
        )

        with UploadProgress(
            self.uploader,
            show_plan=args.plan or args.dry_run,
            show_progress=not args.silent,
            dynamic=not getattr(args, "debug", False),
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

        if failed_entries:
            raise CommandLineError(f"{len(failed_entries)} file(s) failed to upload")

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


class UploadProgress(TransferProgress):
    def __init__(self, uploader: Uploader, **kwargs):
        super().__init__(uploader, "Upload", **kwargs)
