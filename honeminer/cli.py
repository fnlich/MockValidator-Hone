"""Command line entry point: ``python -m honeminer <command>``."""

from __future__ import annotations

import argparse
import json
import sys

from honeminer.config import ConfigError, load_env


def _config(args: argparse.Namespace) -> int:
    settings = load_env(args.env_file)
    print(json.dumps(settings.redacted(), indent=2, default=list))
    if settings.live:
        blockers = settings.live_blockers()
        for problem in blockers:
            print(f"live mode blocked: {problem}", file=sys.stderr)
        return 1 if blockers else 0
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="honeminer", description=__doc__)
    parser.add_argument("--env-file", default=".env", help="settings file (default: .env)")
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config", help="print effective settings, secrets masked")
    config.set_defaults(handler=_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except ConfigError as exc:
        print(f"honeminer: {exc}", file=sys.stderr)
        return 2
