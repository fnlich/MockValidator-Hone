"""Command line entry point: ``python -m honeminer <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from honeminer.config import ConfigError, load_env
from honeminer.pack import RecipeError
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


def _pack(args: argparse.Namespace) -> int:
    from honeminer.grade import GradeEnvironmentError
    from honeminer.pack import RECIPES_DIR, build_and_validate, load_recipe

    settings = load_env(args.env_file)
    names = args.recipes or sorted(p.name for p in RECIPES_DIR.iterdir() if (p / "recipe.json").is_file())
    failures = 0
    for name in names:
        recipe = load_recipe(Path(name) if Path(name).is_dir() else RECIPES_DIR / name)
        output = Path(args.out) / recipe.name
        if output.exists():
            print(f"{recipe.name}: {output} exists, skipped")
            continue
        try:
            result = build_and_validate(recipe, output, image=settings.image)
        except GradeEnvironmentError as exc:
            print(f"cannot build packs here: {exc}", file=sys.stderr)
            return 2
        failures += not result.ok
        print(f"{recipe.name}: reference={result.reference} empty={result.empty} "
              f"deterministic={result.deterministic} -> {'ok' if result.ok else 'INVALID'}")
    return 1 if failures else 0


def _kit(args: argparse.Namespace) -> int:
    import tempfile
    import time

    from honeminer.clock import Clock
    from honeminer.facts import scan
    from honeminer.kit import KitTiming, lint, render_claude_md, render_prompt
    from honeminer.tasks import load_pack

    settings = load_env(args.env_file)
    pack = load_pack(args.pack)
    clock = Clock.for_task(settings)
    timing = KitTiming(time.time(), clock.agent_stop_wall(), settings.time_notices, settings.gate_round_s * 5)
    identity = pack.identity
    with tempfile.TemporaryDirectory(prefix="honeminer-kit-") as scratch:
        root = Path(scratch)
        (root / "s").mkdir()
        work = pack.extract(pack.environment_role, root / "work", root / "s")
        facts = scan(work, task_type=identity.task_type, instruction=identity.instruction,
                     task_kind=getattr(identity, "task_kind", "bug_fix"), language=pack.language,
                     working_directory=getattr(identity, "working_directory", "."),
                     result_tree_path=getattr(identity, "result_tree_path", "."))
    claude_md = render_claude_md(facts, timing)
    prompt = render_prompt(facts, identity.instruction)
    print("===== /task/CLAUDE.md =====")
    print(claude_md)
    print("===== prompt =====")
    print(prompt)
    problems = lint(claude_md, prompt, identity.instruction)
    for problem in problems:
        print(f"lint: {problem}", file=sys.stderr)
    return 1 if problems else 0


def _solve(args: argparse.Namespace) -> int:
    from honeminer.solve import solve_with_claude
    from honeminer.tasks import list_packs, load_pack

    settings = load_env(args.env_file)
    if settings.live:
        print("solve runs local task packs; use `serve` for live offers", file=sys.stderr)
        return 2
    packs = [load_pack(p) for p in (args.packs or [str(p) for p in list_packs(Path(args.dir))])]
    rows = []
    for pack in packs:
        for attempt in range(1, args.runs + 1):
            result = solve_with_claude(pack, settings)
            rows.append((pack.name, attempt, result.grade or "-", result.outcome, result.gate_rounds,
                         result.rank.name.lower()))
            print(f"{pack.name} #{attempt}: grade={result.grade} outcome={result.outcome} "
                  f"gate_rounds={result.gate_rounds} held={result.rank.name.lower()}", flush=True)
    passed = sum(row[2] == "passed" for row in rows)
    print(f"\n{passed}/{len(rows)} passed")
    return 0 if passed == len(rows) else 1


def _rehearse(args: argparse.Namespace) -> int:
    from honeminer.grade import GradeEnvironmentError
    from honeminer.rehearsal import run_rehearsal
    from honeminer.tasks import load_pack

    settings = load_env(args.env_file)
    try:
        rehearsal, root = run_rehearsal(load_pack(args.pack), settings, agent=args.agent, lease_s=args.lease_s,
                                        trajectory_max_bytes=args.trajectory_max_bytes, over_http=args.http)
    except GradeEnvironmentError as exc:
        print(f"cannot grade here: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"honeminer: {exc}", file=sys.stderr)
        return 2
    print(rehearsal.render())
    print(f"\nreport: {root / 'rehearsal.json'}")
    return rehearsal.exit_code


def _serve(args: argparse.Namespace) -> int:
    from honeminer.server import run_server

    settings = load_env(args.env_file)
    if not settings.live:
        print("serve answers live offers: set HONEMINER_MODE=testnet (or mainnet); "
              "use `rehearse` to try a round locally", file=sys.stderr)
        return 2
    run_server(settings)
    return 0


def _doctor(args: argparse.Namespace) -> int:
    from honeminer.doctor import host_checks, report, spike

    settings = load_env(args.env_file)
    checks = host_checks(settings)
    if args.spike:
        checks += spike(settings)
    text, ok = report(checks)
    print(text)
    return 0 if ok else 1


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
    doctor = commands.add_parser("doctor", help="check this host; --spike runs Claude CLI once in the sandbox")
    doctor.add_argument("--spike", action="store_true", help="Phase 0 go/no-go: one real sandboxed Claude run")
    doctor.set_defaults(handler=_doctor)
    kit = commands.add_parser("kit", help="show the CLAUDE.md and prompt generated for a task pack")
    kit.add_argument("pack", help="pack directory or name under packs/")
    kit.set_defaults(handler=_kit)
    solve = commands.add_parser("solve", aliases=["bench"],
                                help="solve task packs with Claude CLI and grade them locally (alias: bench)")
    solve.add_argument("packs", nargs="*", help="pack names or directories (default: every pack in --dir)")
    solve.add_argument("--dir", default="packs", help="where packs live (default: packs)")
    solve.add_argument("--runs", type=int, default=1, help="attempts per pack")
    solve.set_defaults(handler=_solve)
    rehearse = commands.add_parser("rehearse", help="play one whole round locally: problem server, validator, "
                                   "grading and payment (needs Docker)")
    rehearse.add_argument("pack", help="pack directory or name under packs/")
    rehearse.add_argument("--agent", choices=("claude", "reference"), default="claude",
                          help="honeminer's solver: Claude CLI, or the pack's reference answer (no model)")
    rehearse.add_argument("--lease-s", type=int, default=None, help="lease length (HONEMINER_REHEARSAL_LEASE_S)")
    rehearse.add_argument("--trajectory-max-bytes", type=int, default=None,
                          help="trajectory slot size (HONEMINER_REHEARSAL_TRAJECTORY_MAX_BYTES)")
    rehearse.add_argument("--http", action="store_true",
                          help="reach honeminer through the real serve app and rlvr's LiveSolverClient (signed)")
    rehearse.set_defaults(handler=_rehearse)
    serve = commands.add_parser("serve", help="answer live offers from validators (HONEMINER_MODE=testnet|mainnet)")
    serve.set_defaults(handler=_serve)
    pack = commands.add_parser("pack", help="build and self-validate task packs from recipes/ (needs Docker)")
    pack.add_argument("recipes", nargs="*", help="recipe names or directories (default: all)")
    pack.add_argument("--out", default="packs", help="output directory (default: packs)")
    pack.set_defaults(handler=_pack)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (ConfigError, PackError, RecipeError) as exc:
        print(f"honeminer: {exc}", file=sys.stderr)
        return 2
