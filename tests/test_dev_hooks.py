import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "check_file.py"


def run_hook(path: Path) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(path)}})
    return subprocess.run([sys.executable, str(HOOK)], input=payload, capture_output=True, text=True)


def test_python_syntax_error_is_sent_back(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(:\n    pass\n")
    result = run_hook(bad)
    assert result.returncode == 2
    assert "bad.py" in result.stderr


def test_clean_files_are_silent(tmp_path):
    good = tmp_path / "good.py"
    good.write_text("VALUE = 1\n")
    data = tmp_path / "data.json"
    data.write_text('{"a": 1}')
    for path in (good, data):
        result = run_hook(path)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""


def test_invalid_json_and_shell_are_caught(tmp_path):
    data = tmp_path / "data.json"
    data.write_text("{nope")
    script = tmp_path / "s.sh"
    script.write_text("if then fi\n")
    assert run_hook(data).returncode == 2
    assert run_hook(script).returncode == 2


def test_settings_file_is_valid_json_without_permissions():
    settings = json.loads((HOOK.parent.parent / "settings.json").read_text())
    assert "permissions" not in settings
    assert set(settings["hooks"]) == {"PostToolUse", "Stop"}
