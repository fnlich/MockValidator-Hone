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
