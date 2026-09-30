import re
from pathlib import Path

import pytest

from honeminer.cli import main
from honeminer.config import SETTINGS, ConfigError, load_env, parse_dotenv

ROOT = Path(__file__).resolve().parent.parent


def test_defaults_load_without_env_file():
    settings = load_env(None, environ={})
    assert settings.mode == "local"
    assert settings.task_budget_s == 1200
    assert settings.model == "claude-opus-5-5"
    assert settings.time_notices == (0.5, 0.25, 0.1)
    assert settings.agent_memory == 8 * 1024**3
    assert settings.netuid is None
    assert settings.trajectory is True  # the work log is on by default (built after Claude stops)


def test_env_example_lists_every_setting_and_loads():
    text = (ROOT / ".env.example").read_text()
    keys = set(re.findall(r"^([A-Z_][A-Z0-9_]*)=", text, flags=re.M))
    assert keys == {setting.env for setting in SETTINGS.values()}
    settings = load_env(ROOT / ".env.example", environ={})
    assert settings == load_env(None, environ={})


def test_real_environment_beats_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("HONEMINER_SLOTS=3\nHONEMINER_MODEL='claude-sonnet-5-5' # comment\n")
    settings = load_env(env, environ={"HONEMINER_SLOTS": "2"})
    assert settings.slots == 2
    assert settings.model == "claude-sonnet-5-5"


@pytest.mark.parametrize(
    "key,value",
    [
        ("HONEMINER_MODE", "prod"),
        ("HONEMINER_EFFORT", "extreme"),
        ("HONEMINER_SLOTS", "0"),
        ("HONEMINER_TASK_BUDGET_S", "ten"),
        ("HONEMINER_TIME_NOTICES", "0.5,1.5"),
        ("HONEMINER_AGENT_MEMORY", "8 gigs"),
        ("HONEMINER_IMAGE", "ubuntu:latest"),
        ("HONEMINER_TRAJECTORY", "maybe"),
    ],
)
def test_bad_values_fail_loudly(key, value):
    with pytest.raises(ConfigError, match=key):
        load_env(None, environ={key: value})


def test_live_mode_lists_every_blocker():
    settings = load_env(None, environ={"HONEMINER_MODE": "testnet", "HONEMINER_TRAJECTORY": "off"})
    blockers = " ".join(settings.live_blockers())
    for fragment in ("NETUID", "AUTHORIZATION", "TRAJECTORY", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert fragment in blockers
    with pytest.raises(ConfigError, match="live mode refused"):
        settings.require_live()


def test_live_mode_accepts_complete_settings():
    settings = load_env(
        None,
        environ={
            "HONEMINER_MODE": "testnet",
            "NETUID": "7",
            "HONEMINER_ANTHROPIC_AUTHORIZATION": "email 2026-09",
            "HONEMINER_TRAJECTORY": "on",
            "HONEMINER_AUTH": "api_key",
            "ANTHROPIC_API_KEY": "sk-test",
        },
    )
    assert settings.live_blockers() == []


def test_redacted_hides_secrets():
    settings = load_env(None, environ={"ANTHROPIC_API_KEY": "sk-secret", "CLAUDE_CODE_OAUTH_TOKEN": "tok"})
    shown = settings.redacted()
    assert shown["ANTHROPIC_API_KEY"] == "***set***"
    assert "sk-secret" not in repr(shown) and "tok" != shown["CLAUDE_CODE_OAUTH_TOKEN"]


def test_dotenv_rejects_garbage():
    with pytest.raises(ConfigError, match="line 2"):
        parse_dotenv("A=1\nnot a setting\n")


def test_cli_config_prints_and_exits(capsys, tmp_path, monkeypatch):
    for setting in SETTINGS.values():
        monkeypatch.delenv(setting.env, raising=False)
    env = tmp_path / ".env"
    env.write_text("HONEMINER_MODE=testnet\n")
    assert main(["--env-file", str(env), "config"]) == 1
    captured = capsys.readouterr()
    assert '"HONEMINER_MODE": "testnet"' in captured.out
    assert "NETUID is empty" in captured.err
    env.write_text("HONEMINER_SLOTS=zero\n")
    assert main(["--env-file", str(env), "config"]) == 2


def test_cli_errors_are_messages_not_tracebacks(tmp_path, monkeypatch, capsys):
    from honeminer.cli import main

    assert main(["--env-file", "/nonexistent", "grade", "cpp-yamlcpp", str(tmp_path / "missing.diff")]) == 2
    assert "missing.diff" in capsys.readouterr().err
    assert main(["--env-file", "/nonexistent", "rehearse", "cpp-yamlcpp", "--agent", "reference",
                 "--lease-s", "100"]) == 2
    assert "HONEMINER_REHEARSAL_LEASE_S" in capsys.readouterr().err


def test_bench_finds_the_repo_packs_from_any_directory(tmp_path, monkeypatch, capsys):
    from honeminer.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["--env-file", "/nonexistent", "bench", "--runs", "0"]) == 0
    assert "0/0 passed" in capsys.readouterr().out


def test_pack_keeps_only_valid_packs(tmp_path, monkeypatch, capsys):
    import honeminer.pack as pack_module
    from honeminer.cli import main

    outcomes = {"c-ringbuf": True, "rust-rle": False}

    def fake_build(recipe, output, image=""):
        output.mkdir(parents=True)
        (output / "identity.json").write_text("{}")
        ok = outcomes[recipe.name]
        return type("V", (), {"ok": ok, "reference": "passed" if ok else "failed", "empty": "failed",
                              "deterministic": True})()

    monkeypatch.setattr(pack_module, "build_and_validate", fake_build)
    code = main(["--env-file", "/nonexistent", "pack", "c-ringbuf", "rust-rle", "--out", str(tmp_path)])
    assert code == 1 and (tmp_path / "c-ringbuf" / "identity.json").is_file() and not (tmp_path / "rust-rle").exists()
    assert not [p for p in tmp_path.iterdir() if p.name not in ("c-ringbuf",)]
