import json
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from honeminer.facts import scan
from honeminer.kit import KitTiming, lint, render_claude_md, render_prompt, render_settings, write_kit
from honeminer.pack import RECIPES_DIR, load_recipe
from honeminer.tasks import load_pack

TIMING = KitTiming(start_epoch=1000.0, agent_stop_epoch=1000.0 + 1060, time_notices=(0.5, 0.25, 0.1),
                   gate_timeout_s=300)


def yamlcpp_facts(tmp_path):
    pack = load_pack("cpp-yamlcpp")
    (tmp_path / "s").mkdir()
    work = pack.extract("workspace", tmp_path / "w", tmp_path / "s")
    return pack, scan(work, task_type=pack.identity.task_type, instruction=pack.identity.instruction,
                      language=pack.language)


def all_cases(tmp_path):
    pack, facts = yamlcpp_facts(tmp_path)
    yield "cpp-yamlcpp", facts, pack.identity.instruction
    for root in sorted(RECIPES_DIR.iterdir()):
        recipe = load_recipe(root)
        tree = root / ("environment" if recipe.is_terminal else "repo")
        facts = scan(tree, task_type=recipe.task_type, instruction=recipe.instruction, language=recipe.language,
                     task_kind=recipe.task_kind or "bug_fix", result_tree_path=recipe.result_tree_path)
        yield root.name, facts, recipe.instruction


def test_rendered_text_passes_the_prompt_lint_for_every_pack(tmp_path):
    for name, facts, instruction in all_cases(tmp_path):
        claude_md = render_claude_md(facts, TIMING)
        prompt = render_prompt(facts, instruction)
        assert lint(claude_md, prompt, instruction) == [], name
        assert prompt.startswith(f"<task>\n{instruction}\n</task>")
        assert "no one will answer" in claude_md
        assert "About 18 minutes" in claude_md


def test_yamlcpp_claude_md_has_the_verified_facts(tmp_path):
    _, facts = yamlcpp_facts(tmp_path)
    text = render_claude_md(facts, TIMING)
    assert "`python3 .rlvr/build.py`" in text
    assert "libyaml-cpp.a" in text
    assert ".prebuilt/" in text and "Existing test files" in text
    prompt = render_prompt(facts, "x")
    assert "docs/Event-Archives.md" in prompt and "same pattern elsewhere" in prompt


def test_feature_and_terminal_prompts_differ():
    recipe = load_recipe(RECIPES_DIR / "go-feature-wrap")
    facts = scan(recipe.root / "repo", task_type=recipe.task_type, instruction=recipe.instruction,
                 language="go", task_kind="feature")
    assert "The task text is the spec" in render_prompt(facts, recipe.instruction)
    recipe = load_recipe(RECIPES_DIR / "bash-terminal-logreport")
    facts = scan(recipe.root / "environment", task_type=recipe.task_type, instruction=recipe.instruction,
                 result_tree_path=recipe.result_tree_path)
    prompt = render_prompt(facts, recipe.instruction)
    assert "/submission/script.sh" in prompt and "cwd /work/project" in prompt
    assert "mydiff" not in render_claude_md(facts, TIMING)


def test_lint_catches_problems():
    assert lint("MUST do it. NEVER skip.\n", "x", "x")
    assert any("placeholder" in p for p in lint("Build: {build_cmd}\n", "x", "x"))
    assert any("25" in p for p in lint("line\n" * 30, "x", "x"))
    shared = "- a sentence that is long enough to count"
    task = "<task>the instruction</task>"
    assert any("repeated" in p for p in lint(shared + "\n", task + "\n" + shared + "\n", task))


def test_settings_are_hooks_only():
    settings = render_settings(TIMING)
    assert set(settings) == {"hooks"}
    assert settings["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 300


@pytest.fixture
def kit_dir(tmp_path):
    _, facts = yamlcpp_facts(tmp_path)
    return write_kit(tmp_path / "kit", facts, "instruction", TIMING)


def run_hook(kit_dir: Path, name: str, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(kit_dir / "hooks" / name)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=30)


def test_protect_hook(kit_dir):
    blocked = run_hook(kit_dir, "protect.py", {"tool_input": {"file_path": "/work/.rlvr/build.py"}})
    assert blocked.returncode == 2 and "read-only" in blocked.stderr
    protected = json.loads((kit_dir / "kit.json").read_text())["protected"][0]
    assert run_hook(kit_dir, "protect.py", {"tool_input": {"file_path": f"/work/{protected}"}}).returncode == 2
    assert run_hook(kit_dir, "protect.py", {"tool_input": {"file_path": "/work/test/new_case_test.cpp"}}).returncode == 0
    assert run_hook(kit_dir, "protect.py", {"tool_input": {"file_path": "/work/src/eventarchive_store.cpp"}}).returncode == 0
    assert run_hook(kit_dir, "protect.py", {"tool_input": {"file_path": "/task/.kit/kit.json"}}).returncode == 2


def test_check_edit_hook(kit_dir, tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def f(:\n")
    result = run_hook(kit_dir, "check_edit.py", {"tool_input": {"file_path": str(bad)}})
    assert result.returncode == 2 and "Syntax check failed" in result.stderr
    good = tmp_path / "good.py"
    good.write_text("x = 1\n")
    assert run_hook(kit_dir, "check_edit.py", {"tool_input": {"file_path": str(good)}}).returncode == 0
    assert not (tmp_path / "__pycache__").exists()
    assert not list((kit_dir / "hooks").rglob("*.pyc"))


def test_time_notice_fires_once_per_threshold(tmp_path, monkeypatch):
    _, facts = yamlcpp_facts(tmp_path)
    now = time.time()
    timing = KitTiming(start_epoch=now - 800, agent_stop_epoch=now + 200, time_notices=(0.5, 0.25), gate_timeout_s=60)
    kit = write_kit(tmp_path / "k2", facts, "i", timing)
    state = Path("/tmp/.honeminer-time-notices")
    state.unlink(missing_ok=True)
    first = run_hook(kit, "time_notice.py", {})
    assert json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"] == "About 3 minutes left."
    assert run_hook(kit, "time_notice.py", {}).stdout == ""
    state.unlink(missing_ok=True)


class _Gate(BaseHTTPRequestHandler):
    verdict: dict = {}

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps(self.verdict).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_gate_hook_blocks_with_the_gate_reason(kit_dir):
    server = HTTPServer(("127.0.0.1", 0), _Gate)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = json.loads((kit_dir / "kit.json").read_text())
    config["gate_url"] = f"http://127.0.0.1:{server.server_port}/_honeminer/gate"
    (kit_dir / "kit.json").write_text(json.dumps(config))
    try:
        _Gate.verdict = {"decision": "block", "reason": "Not finished: build failed on a clean copy."}
        out = json.loads(run_hook(kit_dir, "gate.py", {"session_id": "s"}).stdout)
        assert out == {"decision": "block", "reason": "Not finished: build failed on a clean copy."}
        _Gate.verdict = {"decision": "allow"}
        assert run_hook(kit_dir, "gate.py", {}).stdout == ""
    finally:
        server.shutdown()


def test_gate_hook_never_traps_claude_when_unreachable(kit_dir):
    config = json.loads((kit_dir / "kit.json").read_text())
    config["gate_url"], config["gate_timeout_s"] = "http://127.0.0.1:9/_honeminer/gate", 2
    (kit_dir / "kit.json").write_text(json.dumps(config))
    result = run_hook(kit_dir, "gate.py", {})
    assert result.returncode == 0 and "unreachable" in result.stdout


def test_run_checks_and_mydiff_are_valid_python(kit_dir):
    for name in ("run_checks.py", "mydiff.py"):
        compile((kit_dir / name).read_text(), name, "exec")
    with tempfile.TemporaryDirectory():
        pass
