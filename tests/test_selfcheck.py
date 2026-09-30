import shutil
import subprocess
from pathlib import Path

import pytest

from honeminer.answer import Rank
from honeminer.facts import scan
from honeminer.pack import RECIPES_DIR, load_recipe
from honeminer.selfcheck import EnvironmentFailure, Exec, GateContext, evaluate


class HostRunner:
    """Test double for the Docker runner: /work, /task/checks and /submission map to host dirs."""

    def apply(self, root: Path, diff: bytes):
        result = subprocess.run(["git", "apply", "-p1", "-"], cwd=root, input=diff, capture_output=True)
        return result.returncode == 0, result.stderr.decode()

    def run(self, root, argv, cwd, *, checks=None, submission=None, timeout_s=300):
        mapped = []
        for arg in argv:
            if checks is not None and arg.startswith("/task/checks/"):
                arg = str(checks / arg.rsplit("/", 1)[1])
            if submission is not None and arg == "/submission/script.sh":
                arg = str(submission / "script.sh")
            mapped.append(arg)
        relative = cwd.removeprefix("/work").lstrip("/")
        result = subprocess.run(mapped, cwd=root / relative, capture_output=True, text=True, timeout=timeout_s)
        return Exec(result.returncode, result.stdout + result.stderr)


MEDIAN_CHECK = """python3 - <<'EOF'
import sys; sys.path.insert(0, '.')
import stats
got = stats.median([4, 1, 3, 2])
print('expected 2.5, got', got)
sys.exit(0 if got == 2.5 else 1)
EOF
"""
PERCENTILE_CHECK = MEDIAN_CHECK.replace("stats.median([4, 1, 3, 2])", "stats.percentile([15, 20, 35, 40, 50], 40)") \
    .replace("expected 2.5", "expected 29.0").replace("got == 2.5", "got == 29.0")


@pytest.fixture
def stats_task(tmp_path):
    recipe = load_recipe(RECIPES_DIR / "python-stats")
    baseline = tmp_path / "baseline"
    shutil.copytree(recipe.root / "repo", baseline)
    subprocess.run(["git", "apply", "-p1", str(recipe.root / "break.diff")], cwd=baseline, check=True)
    work = tmp_path / "work"
    shutil.copytree(baseline, work)
    checks = tmp_path / "checks"
    checks.mkdir()
    facts = scan(baseline, task_type=recipe.task_type, instruction=recipe.instruction, language="python")
    return recipe, GateContext(baseline=baseline, work=work, checks=checks, facts=facts)


def fix(recipe, ctx, full=True):
    fixed = (recipe.root / "repo" / "stats.py").read_text()
    if not full:
        broken = (ctx.baseline / "stats.py").read_text()
        fixed = broken.replace("    return data[mid]\n", "    return (data[mid - 1] + data[mid]) / 2\n")
    (ctx.work / "stats.py").write_text(fixed)


def test_full_fix_with_real_checks_passes(stats_task):
    recipe, ctx = stats_task
    fix(recipe, ctx)
    (ctx.checks / "01-median.sh").write_text(MEDIAN_CHECK)
    (ctx.checks / "02-percentile.sh").write_text(PERCENTILE_CHECK)
    (ctx.checks / "NOTES.md").write_text("not a check")
    verdict = evaluate(ctx, HostRunner())
    assert verdict.passed and verdict.rank is Rank.CHECKED, verdict.message()
    assert verdict.check_results == (("01-median.sh", "fail->pass"), ("02-percentile.sh", "fail->pass"))
    assert b"return (data[mid - 1] + data[mid]) / 2" in verdict.content


def test_no_changes_and_no_checks(stats_task):
    recipe, ctx = stats_task
    assert evaluate(ctx, HostRunner()).stage == "patch"
    fix(recipe, ctx)
    verdict = evaluate(ctx, HostRunner())
    assert not verdict.passed and verdict.stage == "checks" and "at least one check" in verdict.hint
    assert verdict.rank is Rank.BUILDS


def test_half_fix_fails_its_own_check(stats_task):
    recipe, ctx = stats_task
    fix(recipe, ctx, full=False)
    (ctx.checks / "01-median.sh").write_text(MEDIAN_CHECK)
    (ctx.checks / "02-percentile.sh").write_text(PERCENTILE_CHECK)
    verdict = evaluate(ctx, HostRunner())
    assert not verdict.passed and "02-percentile.sh fails on your version" in verdict.hint
    message = verdict.message(minutes_left=7)
    assert message.startswith("Not finished: checks failed on a clean copy.")
    assert "expected 29.0" in message and message.endswith("About 7 minutes left.")


def test_check_that_passes_on_the_original_proves_nothing(stats_task):
    recipe, ctx = stats_task
    fix(recipe, ctx)
    (ctx.checks / "01-trivial.sh").write_text("exit 0\n")
    verdict = evaluate(ctx, HostRunner())
    assert not verdict.passed and "tests nothing" in verdict.hint


def test_bug_fix_check_must_exit_1_on_the_original(stats_task):
    recipe, ctx = stats_task
    fix(recipe, ctx)
    (ctx.checks / "01-crash.sh").write_text("python3 -c 'import stats; assert stats.median([1, 2]) == 1.5' || exit 3\n")
    verdict = evaluate(ctx, HostRunner())
    assert not verdict.passed and "exits 3 on the original" in verdict.hint


def test_protected_edits_are_reported_as_dropped(stats_task):
    recipe, ctx = stats_task
    fix(recipe, ctx)
    (ctx.work / ".rlvr").mkdir()
    (ctx.work / ".rlvr" / "build.py").write_text("print('mine')\n")
    (ctx.checks / "01-median.sh").write_text(MEDIAN_CHECK)
    verdict = evaluate(ctx, HostRunner())
    assert verdict.passed
    assert dict(verdict.dropped)[".rlvr/build.py"].startswith("protected")


def test_environment_failure_is_not_blamed_on_the_patch(stats_task):
    recipe, ctx = stats_task
    fix(recipe, ctx)

    class Broken(HostRunner):
        def apply(self, root, diff):
            raise EnvironmentFailure("docker daemon is not running")

    verdict = evaluate(ctx, Broken())
    assert verdict.env_error and verdict.stage == "environment" and "not with your work" in verdict.hint


@pytest.fixture
def terminal_task(tmp_path):
    recipe = load_recipe(RECIPES_DIR / "bash-terminal-logreport")
    baseline = tmp_path / "baseline"
    shutil.copytree(recipe.root / "environment", baseline)
    submission = tmp_path / "submission"
    submission.mkdir()
    checks = tmp_path / "checks"
    checks.mkdir()
    facts = scan(baseline, task_type=recipe.task_type, instruction=recipe.instruction,
                 result_tree_path=recipe.result_tree_path)
    return recipe, GateContext(baseline=baseline, work=tmp_path / "work", checks=checks, facts=facts,
                               submission=submission)


def test_terminal_script_passes_with_a_real_check(terminal_task):
    recipe, ctx = terminal_task
    ctx.work.mkdir()
    assert evaluate(ctx, HostRunner()).hint == "Write your solution to /submission/script.sh."
    shutil.copy(recipe.root / "reference.sh", ctx.submission / "script.sh")
    (ctx.checks / "01-summary.sh").write_text("grep -qx 'ERROR 4' project/report/summary.txt\n")
    verdict = evaluate(ctx, HostRunner())
    assert verdict.passed, verdict.message()
    (ctx.checks / "02-logs.sh").write_text("test -f project/logs/a.log\n")
    assert "passes without your script" in evaluate(ctx, HostRunner()).hint
