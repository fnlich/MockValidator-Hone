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
