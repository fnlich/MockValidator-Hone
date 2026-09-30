from honeminer.config import load_env
from honeminer.doctor import Check, host_checks, report


def test_report_fails_only_on_required_checks():
    text, ok = report([Check("a", True, "fine"), Check("b", False, "meh", required=False)])
    assert ok and "[warn] b" in text
    text, ok = report([Check("c", False, "broken")])
    assert not ok and "[FAIL] c: broken" in text


def test_host_checks_cover_the_basics(tmp_path):
    settings = load_env(None, environ={"HONEMINER_RUNS_DIR": str(tmp_path / "runs"), "HONEMINER_MODE": "testnet"})
    names = {check.name for check in host_checks(settings)}
    assert {"platform", "docker", "non-root user", "sandbox image", "claude binary", "credential", "disk",
            "live mode"} <= names
    live = next(c for c in host_checks(settings) if c.name == "live mode")
    assert not live.ok and "NETUID" in live.detail


def test_spike_work_log_check(tmp_path):
    from honeminer.config import load_env
    from honeminer.doctor import _spike_work_log
    from honeminer.trajectory import record_line
    from tests.test_trajectory import long_run

    settings = load_env(None, environ={})
    traffic = tmp_path / "traffic.jsonl"
    missing = _spike_work_log(traffic, settings, b"hay.txt\n")
    assert not missing.ok  # a valid but empty log means the gateway recorded nothing
    traffic.write_text("".join(record_line(e.seq, e.path, e.status, e.request, e.response)
                               for e in long_run(2).exchanges))
    check = _spike_work_log(traffic, settings, b"hay.txt\n")
    assert check.ok and "3 model turns" in check.detail


def test_live_mode_needs_the_chain_extras(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_bittensor(name, *args, **kwargs):
        if name == "bittensor":
            raise ImportError("no bittensor")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_bittensor)
    checks = host_checks(load_env(None, environ={"HONEMINER_MODE": "testnet"}))
    chain = next(c for c in checks if c.name == "chain extras")
    assert not chain.ok and "hone-subnet[miner,chain]" in chain.detail


def test_serve_refuses_local_mode(capsys):
    from honeminer.cli import main

    assert main(["--env-file", "/nonexistent", "serve"]) == 2
    assert "HONEMINER_MODE=testnet" in capsys.readouterr().err


def test_serve_refuses_incomplete_live_settings(monkeypatch, capsys):
    from honeminer.cli import main

    monkeypatch.setenv("HONEMINER_MODE", "testnet")
    assert main(["--env-file", "/nonexistent", "serve"]) == 2
    assert "live mode refused" in capsys.readouterr().err


def test_doctor_reports_a_bad_binary_or_runs_dir_instead_of_crashing(tmp_path):
    checks = host_checks(load_env(None, environ={"HONEMINER_CLAUDE_BIN": str(tmp_path),
                                                 "HONEMINER_RUNS_DIR": "/proc/honeminer-nope"}))
    by_name = {c.name: c for c in checks}
    assert not by_name["claude binary"].ok and not by_name["disk"].ok


def test_spike_reports_a_sandbox_that_cannot_start(tmp_path, monkeypatch):
    from honeminer import doctor
    from honeminer.sandbox import AgentSandbox, SandboxError

    fake = tmp_path / "claude"
    fake.write_bytes(b"\x7fELF fake")
    fake.chmod(0o755)

    def refuse(self):
        raise SandboxError("agent container did not start: no Docker daemon")

    monkeypatch.setattr(AgentSandbox, "start", refuse)
    settings = load_env(None, environ={"HONEMINER_CLAUDE_BIN": str(fake), "CLAUDE_CODE_OAUTH_TOKEN": "t",
                                       "HONEMINER_RUNS_DIR": str(tmp_path / "runs")})
    (check,) = doctor.spike(settings)
    assert not check.ok and "no Docker daemon" in check.detail
