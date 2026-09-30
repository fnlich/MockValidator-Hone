"""Command line entry point: ``python -m honeminer <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from honeminer.config import ConfigError, load_env
from honeminer.tasks import PackError


def _config(args: argparse.Namespace) -> int:
    settings = load_env(args.env_file)
    print(json.dumps(settings.redacted(), indent=2, default=list))
    if settings.live:
        blockers = settings.live_blockers()
        for problem in blockers:
            print(f"live mode blocked: {problem}", file=sys.stderr)
        return 1 if blockers else 0
    return 0


def _grade(args: argparse.Namespace) -> int:
    from honeminer.grade import GradeEnvironmentError, grade_submission
    from honeminer.tasks import load_pack

    settings = load_env(args.env_file)
    pack = load_pack(args.pack)
    submission = b"" if args.submission == "-" else Path(args.submission).read_bytes()
    try:
        result = grade_submission(pack, submission, image=settings.image)
    except GradeEnvironmentError as exc:
        print(f"cannot grade here: {exc}", file=sys.stderr)
        return 2
    print(result.render())
    return result.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="honeminer", description=__doc__)
    parser.add_argument("--env-file", default=".env", help="settings file (default: .env)")
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config", help="print effective settings, secrets masked")
    config.set_defaults(handler=_config)
    grade = commands.add_parser("grade", help="grade a patch or script as a validator would")
    grade.add_argument("pack", help="pack directory or name under packs/")
    grade.add_argument("submission", help="unified diff or bash script; '-' for an empty submission")
    grade.set_defaults(handler=_grade)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (ConfigError, PackError) as exc:
        print(f"honeminer: {exc}", file=sys.stderr)
        return 2
