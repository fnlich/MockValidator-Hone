import json

import pytest

from honeminer.answer import Rank
from honeminer.archive import SUMMARY_FIELDS, RunArchive
from honeminer.clock import Clock
from honeminer.config import load_env
from honeminer.pack import RECIPES_DIR, build_pack, load_recipe
from honeminer.runner import AgentResult, claude_argv, claude_env
from honeminer.solve import AUDIT_MESSAGE, solve
from honeminer.tasks import load_pack
from tests.test_pack import host_runner
from tests.test_selfcheck import MEDIAN_CHECK, PERCENTILE_CHECK, HostRunner


@pytest.fixture(scope="module")
def stats_pack(tmp_path_factory):
    out = tmp_path_factory.mktemp("packs") / "python-stats"
    return load_pack(build_pack(load_recipe(RECIPES_DIR / "python-stats"), out, host_runner))


class FakeGateway:
    def __init__(self, gate):
        self.gate = gate
        self.usage = type("U", (), {"input_tokens": 10, "output_tokens": 5, "rate_limited": 0})()

    def stop(self):
        pass


class ScriptedAgent:
    """Plays Claude: each step edits the workspace, then 'stops' (asks the gate)."""

    def __init__(self, dirs, steps, holder, ended="finished"):
        self.dirs, self.steps, self.holder, self.ended = dirs, steps, holder, ended
        self.replies = []

    def run(self, deadline_s):
        for step in self.steps:
            step(self.dirs)
            if step is not self.steps[-1] or self.ended == "finished":
                reply = self.holder["gateway"].gate({"session_id": "s"})
                self.replies.append(reply)
                if reply["decision"] == "allow":
                    break
        return AgentResult(0 if self.ended == "finished" else None, self.ended, 1.0)


def half_fix(dirs):
    text = (dirs.work / "stats.py").read_text()
    (dirs.work / "stats.py").write_text(text.replace("    return data[mid]\n",
                                                     "    return (data[mid - 1] + data[mid]) / 2\n"))
    (dirs.checks / "01-median.sh").write_text(MEDIAN_CHECK)
    (dirs.checks / "02-percentile.sh").write_text(PERCENTILE_CHECK)


def full_fix(dirs):
    text = (dirs.work / "stats.py").read_text()
    (dirs.work / "stats.py").write_text(text.replace(
        "    rank = p / 100 * len(data)\n    rank = min(rank, len(data) - 1)\n",
        "    rank = p / 100 * (len(data) - 1)\n"))


def run_solve(pack, tmp_path, steps, ended="finished", env=None):
    settings = load_env(None, environ={"HONEMINER_RUNS_DIR": str(tmp_path / "runs"), **(env or {})})
    archive = RunArchive.create(tmp_path / "runs", pack.name)
    holder, agents = {}, []

    def gateway_factory(gate, dirs):
        holder["gateway"] = FakeGateway(gate)
        return holder["gateway"]

    def agent_factory(dirs, facts, clock):
        agents.append(ScriptedAgent(dirs, steps, holder, ended))
        return agents[-1]

    result = solve(pack, settings, runner=HostRunner(), agent_factory=agent_factory, archive=archive,
                   clock=Clock.for_task(settings), gateway_factory=gateway_factory)
    return result, agents[0], archive, settings


def test_block_fix_audit_then_ship(stats_pack, tmp_path):
    result, agent, archive, settings = run_solve(stats_pack, tmp_path, [half_fix, full_fix, lambda d: None])
    decisions = [r["decision"] for r in agent.replies]
    assert decisions == ["block", "block", "allow"]
    assert "02-percentile.sh fails on your version" in agent.replies[0]["reason"]
    assert agent.replies[1]["reason"].startswith(AUDIT_MESSAGE)
    assert result.rank is Rank.CHECKED and result.outcome == "finished" and result.gate_rounds == 3
    assert b"rank = p / 100 * (len(data) - 1)" in result.content
    line = json.loads((tmp_path / "runs" / "index.jsonl").read_text().splitlines()[-1])
    assert list(line) == list(SUMMARY_FIELDS)
    assert line["gate_rounds"] == 3 and line["outcome"] == "finished" and line["prompt_version"] == "v3"
    assert (archive.root / "CLAUDE.md").is_file() and (archive.root / "checks" / "01-median.sh").is_file()


def test_audit_round_can_be_disabled(stats_pack, tmp_path):
    result, agent, *_ = run_solve(stats_pack, tmp_path, [half_fix, full_fix],
                                  env={"HONEMINER_AUDIT_ROUND_MIN_LEFT": "0"})
    assert [r["decision"] for r in agent.replies] == ["block", "allow"]


def test_a_looping_agent_is_stopped_and_its_best_answer_ships(stats_pack, tmp_path):
    result, agent, *_ = run_solve(stats_pack, tmp_path, [half_fix, lambda d: None, lambda d: None, lambda d: None])
    assert [r["decision"] for r in agent.replies] == ["block", "block", "allow"]
    assert result.outcome == "stalled" and result.rank is Rank.BUILDS
    assert b"(data[mid - 1] + data[mid]) / 2" in result.content


def test_a_run_killed_at_the_deadline_still_ships_its_final_state(stats_pack, tmp_path):
    result, agent, *_ = run_solve(stats_pack, tmp_path, [lambda d: None, half_fix], ended="deadline")
    assert result.outcome == "deadline"
    assert b"(data[mid - 1] + data[mid]) / 2" in result.content


def test_claude_is_launched_unattended_with_one_model_and_no_credential():
    settings = load_env(None, environ={"HONEMINER_AUTH": "api_key", "ANTHROPIC_API_KEY": "sk-real",
                                       "HONEMINER_MAX_BUDGET_USD": "3"})
    argv = claude_argv(settings, max_budget_usd=settings.max_budget_usd)
    joined = " ".join(argv)
    assert "--permission-mode bypassPermissions" in joined and "--setting-sources user" in joined
    assert "--disallowedTools WebSearch,WebFetch,Agent" in joined and "--max-budget-usd 3.0" in joined
    env = claude_env(settings)
    assert "sk-real" not in json.dumps(env) and env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8080"
    models = {v for k, v in env.items() if k.endswith("_MODEL")}
    assert models == {"claude-opus-5-5"}
    assert env["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"] == "1000"


def test_the_work_log_is_built_from_the_recorded_traffic_and_bound_to_the_answer(stats_pack, tmp_path):
    from rlvr.v3.trajectory import parse_trajectory

    from honeminer.trajectory import record_line
    from tests.test_trajectory import long_run

    def record(dirs):  # what the gateway would have written while Claude worked
        root = dirs.root.parent
        (root / "traffic.jsonl").write_text("".join(
            record_line(e.seq, e.path, e.status, e.request, e.response) for e in long_run(3).exchanges))
        half_fix(dirs)

    result, agent, archive, settings = run_solve(stats_pack, tmp_path, [record, full_fix, lambda d: None])
    log = parse_trajectory((archive.root / "trajectory.json").read_bytes())
    assert log.task_id == stats_pack.task_id and log.model_name == settings.model
    assert log.submission_sha256 == __import__("hashlib").sha256(result.content).hexdigest()
    assert sum(e.event_type == "model_turn" for e in log.events) == 4
    line = json.loads((tmp_path / "runs" / "index.jsonl").read_text().splitlines()[-1])
    assert line["trajectory_ok"] is True and line["trajectory_level"] == "full"
    assert line["trajectory_bytes"] == (archive.root / "trajectory.json").stat().st_size


def test_the_work_log_never_changes_the_answer_or_the_kit(stats_pack, tmp_path):
    shipped = {}
    for mode in ("on", "off"):
        result, agent, archive, _ = run_solve(stats_pack, tmp_path / mode, [half_fix, full_fix, lambda d: None],
                                              env={"HONEMINER_TRAJECTORY": mode})
        shipped[mode] = (result.content, result.rank, [r["decision"] for r in agent.replies],
                         (archive.root / "prompt.txt").read_text(),
                         (archive.root / "CLAUDE.md").read_text())
        assert (archive.root / "trajectory.json").is_file() == (mode == "on")
    assert shipped["on"] == shipped["off"]
    on = load_env(None, environ={"HONEMINER_TRAJECTORY": "on"})
    off = load_env(None, environ={"HONEMINER_TRAJECTORY": "off"})
    assert claude_argv(on) == claude_argv(off) and claude_env(on) == claude_env(off)


def test_a_broken_work_log_is_reported_and_the_answer_still_ships(stats_pack, tmp_path, monkeypatch):
    import honeminer.trajectory as tj

    monkeypatch.setattr(tj, "build_from_traffic", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    result, agent, archive, _ = run_solve(stats_pack, tmp_path, [half_fix, full_fix, lambda d: None])
    assert result.rank is Rank.CHECKED
    line = json.loads((tmp_path / "runs" / "index.jsonl").read_text().splitlines()[-1])
    assert line["trajectory_ok"] is False and "boom" in (archive.root / "trajectory.error.txt").read_text()
