"""Fetch command."""

from __future__ import annotations

import argparse
import os

from cli115.client import Client
from cli115.cmds.base import BaseCommand, WorkerCommand
from cli115.exceptions import CommandLineError
from cli115.fetcher import DEFAULT_CHUNK_SIZE, Fetcher, local_filename
from cli115.helpers import format_size, parse_size
from cli115.cmds.progress import TransferProgress


class FetchCommand(WorkerCommand, BaseCommand):
    """Download a remote file or folder to local disk."""

    client: Client | None = None
    fetcher: Fetcher | None = None

    def register(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("path", nargs="?", help="Remote file or folder path on 115")
        parser.add_argument(
            "--dedup-by-name",
            action="store_true",
            help=(
                "Skip existing local files by destination path "
                "without size or SHA-1 checks"
            ),
        )
        parser.add_argument(
            "--id",
            dest="file_id",
            default=None,
            help="Fetch by remote file/folder ID instead of path",
        )
        parser.add_argument(
            "--chunk-size",
            type=parse_size,
            default=format_size(DEFAULT_CHUNK_SIZE),
            help=(
                "Chunk size for downloading "
                f"(default: {format_size(DEFAULT_CHUNK_SIZE)}, e.g. '4MB', '1048576')"
            ),
        )
        parser.add_argument(
            "-j", "--threads", "--max-workers",
            dest="max_workers", type=int, default=None, metavar="N",
            help="Number of concurrent file downloads for directories (default: 1)",
        )
        parser.add_argument(
            "--check-integrity",
            action="store_true",
            help="Validate file integrity after download",
        )
        parser.add_argument(
            "-o",
            "--output",
            default=None,
            help="Local output path (default: current dir with remote name)",
        )
        parser.add_argument(
            "--plan",
            action="store_true",
            default=False,
            help="Show planned files before downloading",
        )
        parser.add_argument(
            "--include",
            action="append",
            default=None,
            metavar="PATTERN",
            help=(
                "Glob pattern for files to include when downloading a directory "
                "(may be repeated; only matching files are downloaded)"
            ),
        )
        parser.add_argument(
            "--exclude",
            action="append",
            default=None,
            metavar="PATTERN",
            help=(
                "Glob pattern for files to exclude when downloading a directory "
                "(may be repeated; matching files are skipped)"
            ),
        )
        parser.add_argument(
            "-T",
            "--no-target-directory",
            action="store_true",
            default=False,
            help=(
                "Treat output as the exact destination rather than a directory "
                "to download into (never append the remote folder name)"
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Only show files that would be downloaded without downloading",
        )
        parser.add_argument(
            "-s",
            "--silent",
            action="store_true",
            default=False,
            help="Do not report progress, only print the final result",
        )

    def execute(self, args: argparse.Namespace) -> None:
        if not args.file_id and not args.path:
            raise CommandLineError("either 'path' or '--id' is required")
        if args.file_id and args.path:
            raise CommandLineError("use either 'path' or '--id', not both")

        max_workers = args.max_workers
        if max_workers is None:
            max_workers = self.cfg.getint("download", "max_workers", fallback=1)
        if max_workers < 1:
            raise CommandLineError("max workers must be greater than zero")

        self.client = self._create_client()
        self.fetcher = Fetcher(
            self.client,
            dry_run=args.dry_run,
            user_agent=self.cfg["general"]["user_agent"],
            chunk_size=args.chunk_size,
            max_workers=max_workers,
            dedup_by_name=args.dedup_by_name,
        )

        with FetchProgress(
            self.fetcher,
            show_plan=args.plan or args.dry_run,
            show_progress=not args.silent,
        ):
            result = self.run_worker(args)

        failed_entries = [
            entry for entry in self.fetcher.entries if entry.error is not None
        ]
        for entry in failed_entries:
            self.warn(
                "- {0} -> {1}: {2}".format(
                    entry.remote_entry.path,
                    os.fspath(entry.local_path),
                    entry.error,
                )
            )

        if failed_entries:
            raise CommandLineError(f"{len(failed_entries)} file(s) failed to fetch")

        if result and not args.dry_run:
            print(f"Saved to {result}")

    def worker(self, args: argparse.Namespace):
        info = (
            self.client.file.id(args.file_id)
            if args.file_id
            else self.client.file.stat(args.path)
        )

        output = args.output
        if not output:
            output = local_filename(info.name)
        elif os.path.isdir(output):
            if not info.is_directory or not args.no_target_directory:
                output = os.path.join(output, local_filename(info.name))

        check_integrity = args.check_integrity or self.cfg.getboolean(
            "download", "check_integrity", fallback=False
        )

        return self.fetcher.fetch(
            info,
            output,
            check_integrity=check_integrity,
            include=args.include,
            exclude=args.exclude,
        )


class FetchProgress(TransferProgress):
    def __init__(self, fetcher: Fetcher, **kwargs):
        super().__init__(fetcher, "Fetch", **kwargs)
