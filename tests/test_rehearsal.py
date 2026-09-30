import asyncio
import json

import pytest
from rlvr.policy import RELEASE_POLICY
from rlvr.v3.grading import EvaluationResult
from rlvr.v3.patch import PatchLimits
from rlvr.v3.reasons import MinerReason, Stage
from rlvr.v3.round import RoundPolicy
from rlvr.v3.script import ScriptLimits
from rlvr.v3.submission import SubmissionLimits
from rlvr.v3.supervisor import SupervisorPolicy
from rlvr.v3.trajectory import parse_trajectory
from rlvr.v3.tree import TreeLimits

from honeminer import rehearsal as rh
from honeminer.archive import RunArchive
from honeminer.config import load_env
from honeminer.pack import RECIPES_DIR, VERIFIER_POLICY, build_pack, load_recipe
from honeminer.solve import solve
from honeminer.tasks import VERIFIER_LIMITS, WORKSPACE_LIMITS, load_pack
from honeminer.trajectory import Header, build_within
from tests.test_pack import host_runner
from tests.test_selfcheck import HostRunner
from tests.test_solve import FakeGateway, ScriptedAgent, full_fix, half_fix

FIXED = b"rank = p / 100 * (len(data) - 1)"


@pytest.fixture(scope="module")
def stats_pack(tmp_path_factory):
    out = tmp_path_factory.mktemp("packs") / "python-stats"
    return load_pack(build_pack(load_recipe(RECIPES_DIR / "python-stats"), out, host_runner))


def round_policy_for_tests(tmp_path):
    """The release round policy without its root/Docker checks (grading is faked in these tests)."""

    return RoundPolicy(
        workspace_archive=WORKSPACE_LIMITS, verifier_archive=VERIFIER_LIMITS,
        submissions=SubmissionLimits(RELEASE_POLICY.v3_patch_bytes, RELEASE_POLICY.v3_script_bytes),
        tree=TreeLimits(200_000, 1 << 30, 1 << 32, 4096), patch=PatchLimits(RELEASE_POLICY.v3_patch_bytes, 60),
        script=ScriptLimits(RELEASE_POLICY.v3_script_bytes),
        supervisor=SupervisorPolicy(image="registry.invalid/profile@sha256:" + "a" * 64, candidate_uid=65534,
                                    candidate_gid=65534, memory_bytes=256 << 20, cpus=1, pids_limit=64,
                                    tmpfs_bytes=64 << 20, max_file_bytes=1 << 20, watchdog_slack_s=2,
                                    max_workspace_bytes=1 << 30),
        docker_binary=str(tmp_path / "docker"), artifact_origins=frozenset(),
        execution_profile_id=RELEASE_POLICY.v3_execution_profile_id, verifier_policy=VERIFIER_POLICY,
        dispatch_concurrency=1,
    )



@pytest.fixture
def fake_grading(monkeypatch):
    """Grades by content, the way rlvr's own round tests do: the full fix passes, anything else fails."""

    graded = []

    def grade(workspace, patch, *args, **kwargs):
        graded.append(patch)
        if patch.startswith(b"this is not a patch"):
            return EvaluationResult("rejected", "patch was rejected", (), None)
        if FIXED in patch and b"(data[mid - 1] + data[mid]) / 2" in patch:
            return EvaluationResult("passed", "", (), None)
        return EvaluationResult("failed", "check 02-percentile failed", (), None, MinerReason.CHECK_FAILED,
                                Stage.CHECK, failed_check="02-percentile")

    monkeypatch.setattr("rlvr.v3.round.evaluate_repository", grade)
    return graded


def scripted_solve(tmp_path, steps, settings):
    """honeminer's real solve() with the scripted agent from test_solve (no model, no Docker)."""

    def solve_fn(pack, clock, spec):
        archive = RunArchive.create(tmp_path / "runs", pack.name)
        holder = {}

        def gateway_factory(gate, dirs):
            holder["gateway"] = FakeGateway(gate)
            return holder["gateway"]

        return solve(pack, settings, runner=HostRunner(), archive=archive, clock=clock, work_log=spec,
                     agent_factory=lambda dirs, facts, clock: ScriptedAgent(dirs, steps, holder),
                     gateway_factory=gateway_factory)

    return solve_fn


def run(pack, tmp_path, *, steps=(half_fix, full_fix, lambda d: None), env=None, solve_fn=None, **kwargs):
    settings = load_env(None, environ={"HONEMINER_RUNS_DIR": str(tmp_path / "runs"), **(env or {})})
    solve_fn = solve_fn or scripted_solve(tmp_path, list(steps), settings)
    answer = rh.answer_with(settings, solve_fn, tmp_path / "miner")
    kwargs.setdefault("lease_s", 1500)
    kwargs.setdefault("trajectory_max_bytes", 64 * 1024**2)
    return asyncio.run(rh.rehearse(pack, answer=answer, policy=round_policy_for_tests(tmp_path),
                                   workdir=tmp_path / "round", **kwargs))


def row(rehearsal, name):
    return next(r for r in rehearsal.rows if r["miner"] == name)


def test_a_full_round_pays_honeminer(stats_pack, tmp_path, fake_grading):
    rehearsal = run(stats_pack, tmp_path)
    assert rehearsal.result.status == "completed", rehearsal.result.reason
    ours = row(rehearsal, rh.HONEMINER)
    assert (ours["reply"], ours["commit"], ours["grade"]) == ("sent", "grant", "passed")
    assert 0.95 <= ours["payment"] <= 1.0 and rehearsal.exit_code == 0
    assert (row(rehearsal, rh.REFERENCE)["grade"], row(rehearsal, rh.EMPTY)["grade"],
            row(rehearsal, rh.BROKEN)["grade"]) == ("passed", "failed", "rejected")
    assert row(rehearsal, rh.COPYCAT)["payment"] < row(rehearsal, rh.REFERENCE)["payment"]  # slower pass, paid less
    silent = row(rehearsal, rh.SILENT)
    assert (silent["reply"], silent["commit"], silent["payment"]) == ("none", "artifact_invalid", 0.0)
    # The work log that was uploaded is bound to this round and to the answer that was graded.
    reply = rehearsal.honeminer.reply
    log = parse_trajectory(reply.trajectory)
    assert (log.challenge_id, log.miner_hotkey) == (rehearsal.server.challenge_id, rh.hotkey_for(rh.HONEMINER))
    assert reply.submission in fake_grading and FIXED in reply.submission
    report = rehearsal.to_json()
    assert json.loads(json.dumps(report))["round"]["status"] == "completed" and "honeminer" in rehearsal.render()


def test_a_tiny_trajectory_slot_shrinks_the_log_and_still_gets_a_grant(stats_pack, tmp_path, fake_grading):
    def noisy_fix(dirs):  # traffic recorded while Claude worked, big enough to need shrinking
        from honeminer.trajectory import record_line
        from tests.test_trajectory import long_run

        (dirs.root.parent / "traffic.jsonl").write_text("".join(
            record_line(e.seq, e.path, e.status, e.request, e.response) for e in long_run(30, 5_000).exchanges))
        half_fix(dirs)

    rehearsal = run(stats_pack, tmp_path, steps=(noisy_fix, full_fix, lambda d: None), trajectory_max_bytes=12_000)
    assert row(rehearsal, rh.HONEMINER)["commit"] == "grant" and rehearsal.exit_code == 0
    assert len(rehearsal.honeminer.reply.trajectory) <= 12_000


def test_a_limit_below_the_log_floor_gets_an_explicit_error_and_no_upload(stats_pack, tmp_path, fake_grading):
    rehearsal = run(stats_pack, tmp_path, env={"HONEMINER_TRAJECTORY_MAX_BYTES": "600"})
    ours = row(rehearsal, rh.HONEMINER)
    assert ours["reply"] == "none" and ours["commit"] == "artifact_invalid" and ours["payment"] == 0.0
    assert "ReplyError" in rehearsal.honeminer.error and rehearsal.exit_code == 1
    assert all(u.data is None for u in rehearsal.server.uploads.values() if "-1-" in u.slot.upload_id)


@pytest.mark.parametrize("tamper", ["challenge_id", "miner_hotkey", "submission_sha256"])
def test_a_log_bound_to_anything_else_is_trajectory_invalid(stats_pack, tmp_path, fake_grading, tamper):
    def tampered(task, hotkey, content):
        header = Header(task_id=task.task_id, challenge_id="other" if tamper == "challenge_id" else task.challenge_id,
                        miner_hotkey="other" if tamper == "miner_hotkey" else hotkey, model_name="m")
        return build_within([], header, b"x" if tamper == "submission_sha256" else content,
                            task.slots.trajectory.max_bytes, timeout_s=5).data

    rehearsal = run(stats_pack, tmp_path, stand_in_logs={rh.REFERENCE: tampered})
    reference = row(rehearsal, rh.REFERENCE)
    assert reference["commit"] == "trajectory_invalid" and tamper in reference["detail"]
    assert reference["payment"] == 0.0 and reference["grade"] == "rejected"  # never graded, even though correct
    assert rehearsal.exit_code == 0


def test_upload_failures_are_retried(stats_pack, tmp_path, fake_grading):
    rehearsal = run(stats_pack, tmp_path, put_faults=2)
    assert rehearsal.result.status == "completed" and rehearsal.exit_code == 0
    assert len([line for line in rehearsal.server.log if "503" in line]) == 2


def test_a_short_lease_shrinks_the_agent_window(stats_pack, tmp_path, fake_grading):
    seen = {}

    def spy_solve(pack, clock, spec):
        seen["agent_s"] = clock.agent_remaining()
        return rh.reference_solve(stats_pack.reference(), load_env(None, environ={}))(pack, clock, spec)

    rehearsal = run(stats_pack, tmp_path, lease_s=700, solve_fn=spy_solve)
    assert rehearsal.exit_code == 0 and seen["agent_s"] < 700 - 45 - 55 + 1


def test_the_reference_agent_rehearses_the_plumbing_without_a_model(stats_pack, tmp_path, fake_grading):
    settings = load_env(None, environ={})
    rehearsal = run(stats_pack, tmp_path, solve_fn=rh.reference_solve(stats_pack.reference(), settings))
    assert row(rehearsal, rh.HONEMINER)["grade"] == "passed" and rehearsal.exit_code == 0


def test_when_no_miner_can_fit_its_log_the_round_is_abandoned_for_quorum(stats_pack, tmp_path, fake_grading):
    rehearsal = run(stats_pack, tmp_path, trajectory_max_bytes=600)
    assert rehearsal.result.status == "abandoned" and "quorum" in rehearsal.result.reason
    assert rehearsal.exit_code == 2
