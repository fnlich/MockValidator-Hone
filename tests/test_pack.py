import subprocess
from pathlib import Path

import pytest
from rlvr.v3.manifest import load_manifest

from honeminer.pack import RECIPES_DIR, RecipeError, RunOutput, build_pack, gold_fingerprint, load_recipe
from honeminer.tasks import load_pack


def host_runner(root: Path, argv, cwd: str, stdin: bytes, timeout_s: int, submission=None) -> RunOutput:
    """Runs a check on the host with /work mapped to ``root`` (tests only; packs use Docker)."""

    relative = cwd.removeprefix("/work").lstrip("/")
    if submission is not None:
        argv = [str(submission / "script.sh") if a == "/submission/script.sh" else a for a in argv]
    result = subprocess.run(argv, cwd=root / relative, input=stdin, capture_output=True, timeout=timeout_s)
    return RunOutput(result.returncode, result.stdout)


def test_every_recipe_loads():
    names = sorted(p.name for p in RECIPES_DIR.iterdir() if (p / "recipe.json").is_file())
    assert len(names) >= 10
    languages = {load_recipe(RECIPES_DIR / name).language for name in names}
    assert {"c", "rust", "go", "python", "javascript", "java", "bash"} <= languages
    for name in names:
        recipe = load_recipe(RECIPES_DIR / name)
        assert len(recipe.checks) >= 2


@pytest.mark.parametrize("name", ["python-stats", "bash-terminal-logreport"])
def test_built_pack_matches_hone_format(name, tmp_path):
    recipe = load_recipe(RECIPES_DIR / name)
    pack = load_pack(build_pack(recipe, tmp_path / name, host_runner))
    assert pack.is_terminal == recipe.is_terminal
    (tmp_path / "s").mkdir()
    verifier = pack.extract("verifier", tmp_path / "verifier", tmp_path / "s")
    manifest = load_manifest(verifier, task_type=recipe.task_type)
    assert [check.check_id for check in manifest.checks] == [check.check_id for check in recipe.checks]
    work = pack.extract(pack.environment_role, tmp_path / "work", tmp_path / "s")
    if not recipe.is_terminal:
        check = subprocess.run(["git", "apply", "--check", "-p1", "-"], cwd=work, input=pack.reference())
        assert check.returncode == 0
        assert b"return (data[mid - 1] + data[mid]) / 2" in pack.reference()
    else:
        assert pack.reference() == (recipe.root / "reference.sh").read_bytes()


def test_builds_are_deterministic(tmp_path):
    recipe = load_recipe(RECIPES_DIR / "python-stats")
    first = build_pack(recipe, tmp_path / "a", host_runner)
    second = build_pack(recipe, tmp_path / "b", host_runner)
    assert gold_fingerprint(first) == gold_fingerprint(second)
    assert (first / "task_id.txt").read_text() == (second / "task_id.txt").read_text()


def test_bad_recipes_are_refused(tmp_path):
    (tmp_path / "recipe.json").write_text('{"instruction": "x", "checks": [{"id": "01-a"}]}')
    with pytest.raises(RecipeError, match="checks/01-a.py"):
        load_recipe(tmp_path)
    (tmp_path / "checks").mkdir()
    (tmp_path / "checks" / "01-a.py").write_text("print(1)\n")
    with pytest.raises(RecipeError, match="repo"):
        load_recipe(tmp_path)
