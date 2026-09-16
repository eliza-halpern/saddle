"""Saddle command line interface.

Scaffold: version only. Subcommands land with later vertical-slice issues.
"""

from __future__ import annotations

import argparse

from saddle import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="saddle", description="Deterministic harness for local LLMs."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
