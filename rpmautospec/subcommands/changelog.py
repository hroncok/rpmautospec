from typing import Any, Optional, Union

from ..pkg_history import PkgHistoryProcessor
from .. import pager


def register_subcommand(subparsers):
    subcmd_name = "generate-changelog"

    gen_changelog_parser = subparsers.add_parser(
        subcmd_name,
        help="Generate changelog entries from git commit logs",
    )

    gen_changelog_parser.add_argument(
        "spec_or_path",
        default=".",
        nargs="?",
        help="Path to package worktree or the spec file within",
    )

    return subcmd_name


def _coerce_to_str(str_or_bytes: Union[str, bytes]) -> str:
    if isinstance(str_or_bytes, bytes):
        str_or_bytes = str_or_bytes.decode("utf-8", errors="replace")
    return str_or_bytes


def _coerce_to_bytes(str_or_bytes: Union[str, bytes]) -> str:
    if isinstance(str_or_bytes, str):
        str_or_bytes = str_or_bytes.encode("utf-8")
    return str_or_bytes


def collate_changelog(
    processor_results: dict[str, Any], result_type: Optional[type] = str
) -> Union[str, bytes]:
    changelog = processor_results["changelog"]
    if result_type == str:
        entry_strings = (_coerce_to_str(entry.format()) for entry in changelog)
    else:
        entry_strings = (_coerce_to_bytes(entry.format()) for entry in changelog)
    return "\n\n".join(entry_strings)


def produce_changelog(spec_or_repo):
    processor = PkgHistoryProcessor(spec_or_repo)
    result = processor.run(visitors=(processor.release_number_visitor, processor.changelog_visitor))
    return collate_changelog(result)


def main(args):
    """Main method."""
    changelog = produce_changelog(args.spec_or_path)
    pager.page(changelog, enabled=args.pager)
