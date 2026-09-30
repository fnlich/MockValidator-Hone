"""The gate on the real yaml-cpp task, in grading containers (CI docker job)."""

import shutil
import subprocess

import pytest

from honeminer.config import load_env
from honeminer.facts import scan
from honeminer.selfcheck import DockerGateRunner, GateContext, evaluate
from honeminer.tasks import load_pack

pytestmark = pytest.mark.docker


@pytest.fixture
def yamlcpp(tmp_path):
    pack = load_pack("cpp-yamlcpp")
    (tmp_path / "s").mkdir()
    baseline = pack.extract("workspace", tmp_path / "baseline", tmp_path / "s")
    verifier = pack.extract("verifier", tmp_path / "verifier", tmp_path / "s")
    work = tmp_path / "work"
    shutil.copytree(baseline, work)
    checks = tmp_path / "checks"
    checks.mkdir()
    # One of Hone's own hidden checks, turned into a repro check: compare with its gold output.
    source = next((verifier / "checks").glob("03-*.py"))
    shutil.copy(source, checks / "hidden.py")
    shutil.copy(verifier / "gold" / f"{source.stem}.txt", checks / "hidden.gold")
    (checks / "01-nul-roundtrip.sh").write_text(
        "python3 /task/checks/hidden.py > /tmp/out.txt\n"
        "cmp -s /tmp/out.txt /task/checks/hidden.gold && exit 0\n"
        "diff /task/checks/hidden.gold /tmp/out.txt | head -20; exit 1\n"
    )
    facts = scan(baseline, task_type=pack.identity.task_type, instruction=pack.identity.instruction, language="cpp")
    return pack, GateContext(baseline=baseline, work=work, checks=checks, facts=facts)


def test_reference_fix_passes_the_gate(yamlcpp):
    pack, ctx = yamlcpp
    subprocess.run(["git", "apply", "-p1", "-"], cwd=ctx.work, input=pack.reference(), check=True)
    verdict = evaluate(ctx, DockerGateRunner(load_env().image))
    assert verdict.passed, verdict.message()
    assert b".prebuilt" not in verdict.content


def test_half_fix_fails_the_gate(yamlcpp):
    pack, ctx = yamlcpp
    reference = pack.reference().decode()
    half = reference[: reference.index("--- a/src/eventarchive_store.cpp")].encode()
    subprocess.run(["git", "apply", "-p1", "-"], cwd=ctx.work, input=half, check=True)
    verdict = evaluate(ctx, DockerGateRunner(load_env().image))
    assert not verdict.passed and not verdict.env_error, verdict.message()
