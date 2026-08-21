"""Command-line interface for the far-aim pipeline (plan §18).

Phase 0 status: `check` and `validate` operate on the source manifest;
the remaining commands are registered stubs that exit with code 2 until
their phase is implemented.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from far_aim import __version__
from far_aim.config import Config
from far_aim.log import setup_logging
from far_aim.manifest import ManifestError, SourceManifest

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_IMPLEMENTED = 2

CORPORA = ("ecfr", "aim", "pcg")

NOT_IMPLEMENTED_PHASE = {
    ("fetch", "ecfr"): "Phase 1",
    ("parse", "ecfr"): "Phase 2",
    ("normalize", None): "Phase 2",
    ("diff", None): "Phase 2",
    ("build-vault", None): "Phase 3",
    ("fetch", "aim"): "Phase 4",
    ("parse", "aim"): "Phase 4",
    ("fetch", "pcg"): "Phase 5",
    ("parse", "pcg"): "Phase 5",
    ("update", None): "Phase 8",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="far-aim",
        description="Deterministic FAR/AIM/PCG ingestion pipeline and Obsidian vault generator.",
    )
    parser.add_argument("--version", action="version", version=f"far-aim {__version__}")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="project root directory (default: current directory)",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="report source registry state")
    fetch = sub.add_parser("fetch", help="download a source corpus")
    fetch.add_argument("corpus", choices=CORPORA)
    parse = sub.add_parser("parse", help="parse an archived raw corpus")
    parse.add_argument("corpus", choices=CORPORA)
    sub.add_parser("normalize", help="normalize parsed data into canonical JSON")
    sub.add_parser("validate", help="validate manifest and normalized data")
    sub.add_parser("diff", help="structural diff between accepted source versions")
    sub.add_parser("build-vault", help="generate the Obsidian vault from canonical data")
    sub.add_parser("update", help="run the full check→fetch→parse→validate→diff→generate sequence")
    return parser


def cmd_check(config: Config) -> int:
    path = config.manifest_path
    if path.exists():
        try:
            manifest = SourceManifest.load(path)
        except ManifestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        print(f"Source registry: {path}")
    else:
        manifest = SourceManifest.default()
        print(f"Source registry: {path} (not created yet — showing defaults)")
    print()
    rows = [("source", "accepted version", "last checked")]
    for name, state in sorted(manifest.sources.items()):
        accepted = state.accepted_version or state.effective_date or "-"
        if state.change is not None:
            accepted = f"{accepted} (change {state.change})"
        rows.append((name, accepted, state.last_checked_at or "never"))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    for row in rows:
        line = "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        print(line.rstrip())
    print()
    print("note: upstream polling is not implemented yet (arrives in Phase 1).")
    return EXIT_OK


def cmd_validate(config: Config) -> int:
    path = config.manifest_path
    if not path.exists():
        print(f"error: source registry missing: {path}", file=sys.stderr)
        return EXIT_ERROR
    try:
        SourceManifest.load(path)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"ok: manifest schema valid ({path})")
    print("note: normalized-data validation arrives with Phase 2.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    config = Config.load(args.root)

    if args.command == "check":
        return cmd_check(config)
    if args.command == "validate":
        return cmd_validate(config)

    corpus = getattr(args, "corpus", None)
    phase = NOT_IMPLEMENTED_PHASE[(args.command, corpus)]
    target = f"{args.command} {corpus}" if corpus else args.command
    print(f"far-aim {target}: not implemented yet — planned for {phase}.", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    sys.exit(main())
