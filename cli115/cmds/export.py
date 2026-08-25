"""Export command – exports a remote directory tree structure."""

from __future__ import annotations

import argparse
import os
import sys
from cli115.cmds.base import BaseCommand
from cli115.exceptions import CommandLineError
from cli115.helpers import format_size


def build_tree_text(
    client, root_dir, *, show_size: bool = True
) -> tuple[str, int, int]:
    """Recursively traverse a remote directory and build a standard tree text.

    Returns:
        tuple of (tree_text, total_directories, total_files)
    """
    lines = [root_dir.name or "/"]
    dir_count = 0
    file_count = 0

    def _walk(current_dir, prefix: str = "") -> None:
        nonlocal dir_count, file_count
        try:
            items = list(client.file.list(current_dir))
        except Exception:
            return

        dirs = [item for item in items if item.is_directory]
        files = [item for item in items if not item.is_directory]
        sorted_items = dirs + files
        total_items = len(sorted_items)

        for index, item in enumerate(sorted_items):
            is_last = index == total_items - 1
            connector = "└── " if is_last else "├── "
            child_prefix = prefix + ("    " if is_last else "│   ")

            if item.is_directory:
                dir_count += 1
                lines.append(f"{prefix}{connector}{item.name}/")
                _walk(item, child_prefix)
            else:
                file_count += 1
                if show_size and getattr(item, "size", None) is not None:
                    lines.append(
                        f"{prefix}{connector}{item.name} ({format_size(item.size)})"
                    )
                else:
                    lines.append(f"{prefix}{connector}{item.name}")

    _walk(root_dir, "")
    summary = f"\n{dir_count} directories, {file_count} files"
    lines.append(summary)
    return "\n".join(lines), dir_count, file_count


class ExportCommand(BaseCommand):
    """Export a remote directory tree structure to a text file."""

    def register(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("path", nargs="?", help="Remote directory path on 115")
        parser.add_argument(
            "--id",
            dest="dir_id",
            default=None,
            help="Export by remote directory ID instead of path",
        )
        parser.add_argument(
            "-o",
            "--output",
            default=None,
            help="Local output file path (default: <dir_name>_目录树.txt, or '-' for stdout)",
        )
        parser.add_argument(
            "--server",
            action="store_true",
            default=False,
            help="Use 115 server-side asynchronous export instead of client-side traversal",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=60.0,
            help="Timeout in seconds for waiting for server export task (default: 60)",
        )
    def execute(self, args: argparse.Namespace) -> None:
        if not args.dir_id and not args.path:
            raise CommandLineError("either path or --id is required")

        client = self._create_client()
        dir_entry = (
            client.file.id(args.dir_id) if args.dir_id else client.file.stat(args.path)
        )

        if not dir_entry.is_directory:
            raise CommandLineError(f"'{args.path or args.dir_id}' is not a directory")

        if args.server:
            if args.output != "-":
                print(f"Submitting server-side export task for '{dir_entry.path or dir_entry.name}'...")
            export_result = client.file.export_dir(dir_entry, timeout=args.timeout)
            content = export_result.get("content", "")
            file_url = (
                export_result.get("file_url")
                or export_result.get("download_url")
                or export_result.get("url")
            )
            if not content and file_url:
                resp = client.file._api.get(file_url)
                content = resp.text
        else:
            if args.output != "-":
                print(f"Building directory tree for '{dir_entry.path or dir_entry.name}'...")
            content, d_count, f_count = build_tree_text(client, dir_entry)

        # Determine output file path
        output_path = args.output
        if not output_path:
            clean_name = (
                dir_entry.name.replace("/", "").replace("\\", "").strip() or "root"
            )
            output_path = f"{clean_name}_目录树.txt"

        if content:
            if output_path == "-":
                sys.stdout.write(content + "\n")
            else:
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(content)
                print(
                    f"Directory tree exported successfully to: {os.path.abspath(output_path)}"
                )
        else:
            print("Failed to generate directory tree content.")
