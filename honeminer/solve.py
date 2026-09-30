"""Solve one task end to end: prepare -> facts -> kit -> Claude -> gate -> keeper -> archive.

The gate answers the Stop hook through the gateway: it re-checks the work on
clean copies, feeds every result to the answer keeper, blocks with the exact
failure while there is time, asks for one audit round when a pass comes early,
and lets Claude stop when the time is up or the run has stalled.
"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from honeminer.answer import AnswerKeeper, Rank
from honeminer.archive import RunArchive
from honeminer.clock import Clock
from honeminer.config import ConfigError, Settings
from honeminer.facts import Facts, scan
from honeminer.guards import StallGuard
from honeminer.kit import PROMPT_VERSION, KitTiming, write_kit
from honeminer.patch import PatchBuildError, build_patch
from honeminer.runner import Agent, AgentResult
from honeminer.selfcheck import GateContext, GateRunner, evaluate, list_checks
from honeminer.tasks import TaskPack

AUDIT_MESSAGE = ("Your checks pass. Before finishing, re-read the documentation of the component the task names: "
                 "is every promise it makes covered by a check? Add checks for any that are not, then stop again.")


@dataclass
class TaskDirs:
    root: Path
    work: Path
    baseline: Path
    checks: Path
    submission: Path
    kit: Path


@dataclass
class GateState:
    rounds: int = 0
    audited: bool = False
    stalled: bool = False
    last_message: str = ""
    history: list = field(default_factory=list)


class Gate:
    """The Stop hook's counterpart on the host."""

    def __init__(self, ctx: GateContext, runner: GateRunner, keeper: AnswerKeeper, clock: Clock,
                 settings: Settings) -> None:
        self.ctx, self.runner, self.keeper, self.clock, self.settings = ctx, runner, keeper, clock, settings
        self.state = GateState()
        self.guard = StallGuard()
        self._lock = threading.Lock()

    def __call__(self, request: dict) -> dict:
        with self._lock:
            return self._decide()

    def final_check(self) -> None:
        """Check the work tree Claude left, inside the final-check reserve.

        Taking the lock first also waits for a gate round still in flight, so its verdict is in the keeper
        before the answer is chosen.
        """

        with self._lock:
            if self.clock.final_check_remaining() < 1:
                return
            verdict = evaluate(self.ctx, self.runner)
            if verdict.content and not verdict.env_error:
                self.keeper.offer(verdict.content, verdict.rank, "final check")
            self.state.history.append({"round": "final", "passed": verdict.passed, "stage": verdict.stage,
                                       "hint": verdict.hint, "env_error": verdict.env_error,
                                       "seconds_left": round(self.clock.final_check_remaining())})

    def _decide(self) -> dict:
        if not self.clock.can_start(self.settings.gate_round_s):
            return {"decision": "allow", "note": "time is up"}
        self.state.rounds += 1
        verdict = evaluate(self.ctx, self.runner)
        if verdict.content:
            self.keeper.offer(verdict.content, verdict.rank, verdict.stage)
        minutes = max(1, round(self.clock.agent_remaining() / 60))
        self.state.history.append({"round": self.state.rounds, "passed": verdict.passed, "stage": verdict.stage,
                                   "hint": verdict.hint, "env_error": verdict.env_error,
                                   "seconds_left": round(self.clock.agent_remaining())})
        if verdict.passed:
            if (not self.state.audited and self.settings.audit_round_min_left > 0
                    and self.clock.fraction_left() > self.settings.audit_round_min_left):
                self.state.audited = True
                return {"decision": "block", "reason": f"{AUDIT_MESSAGE} About {minutes} minutes left."}
            return {"decision": "allow"}
        if verdict.env_error:
            return {"decision": "allow", "note": "gate environment error"}
        if self.guard.observe(verdict.content, verdict.stage, verdict.hint):
            self.state.stalled = True
            return {"decision": "allow", "note": "stalled"}
        self.state.last_message = verdict.message(minutes)
        return {"decision": "block", "reason": self.state.last_message}


def prepare(pack: TaskPack, root: Path) -> tuple[TaskDirs, Facts]:
    scratch = root / "scratch"
    scratch.mkdir(parents=True)
    work = pack.extract(pack.environment_role, root / "work", scratch)
    baseline = root / "baseline"
    shutil.copytree(work, baseline, symlinks=True)
    dirs = TaskDirs(root, work, baseline, root / "checks", root / "submission", root / "kit")
    dirs.checks.mkdir()
    dirs.submission.mkdir()
    identity = pack.identity
    facts = scan(work, task_type=identity.task_type, instruction=identity.instruction,
                 task_kind=getattr(identity, "task_kind", "bug_fix"), language=pack.language,
                 working_directory=getattr(identity, "working_directory", "."),
                 result_tree_path=getattr(identity, "result_tree_path", "."))
    return dirs, facts


@dataclass(frozen=True)
class SolveResult:
    content: bytes
    rank: Rank
    outcome: str
    gate_rounds: int
    agent: AgentResult | None
    grade: str | None = None
    grade_reason: str = ""
    trajectory: bytes | None = None  # the work log for ``content``; None when it is off or could not be built
    trajectory_error: str = ""


@dataclass(frozen=True)
class WorkLogSpec:
    """Who the work log is for: a live offer's header and its trajectory slot size."""

    header: object  # honeminer.trajectory.Header
    max_bytes: int


AgentFactory = Callable[[TaskDirs, Facts, Clock], Agent]


def solve(pack: TaskPack, settings: Settings, *, runner: GateRunner, agent_factory: AgentFactory,
          archive: RunArchive, clock: Clock | None = None, gateway_factory=None,
          grader: Callable[[TaskPack, bytes], tuple[str, str]] | None = None,
          work_log: WorkLogSpec | None = None) -> SolveResult:
    clock = clock or Clock.for_task(settings)
    started = time.monotonic()
    dirs, facts = prepare(pack, archive.root / "task")
    facts, baseline_tests_passed = verify_facts(facts, dirs.baseline, runner, dirs.root, clock, settings)
    timing = KitTiming(start_epoch=time.time(), agent_stop_epoch=clock.agent_stop_wall(),
                       time_notices=settings.time_notices,
                       gate_timeout_s=max(60, int(clock.agent_remaining())))
    write_kit(dirs.kit, facts, pack.identity.instruction, timing)
    archive.write("facts.json", facts.to_json())
    archive.write("CLAUDE.md", (dirs.kit / "CLAUDE.md").read_text())
    archive.write("prompt.txt", (dirs.kit / "prompt.txt").read_text())

    keeper = AnswerKeeper()
    ctx = GateContext(baseline=dirs.baseline, work=dirs.work, checks=dirs.checks, facts=facts,
                      submission=dirs.submission if facts.is_terminal else None,
                      baseline_tests_passed=baseline_tests_passed, time_left=clock.final_check_remaining)
    gate = Gate(ctx, runner, keeper, clock, settings)
    gateway = gateway_factory(gate, dirs) if gateway_factory else None
    agent_result = None
    try:
        agent_result = agent_factory(dirs, facts, clock).run(clock.agent_remaining())
    finally:
        if gateway is not None:
            archive.write("usage.json", gateway.usage.__dict__)
            gateway.stop()

    # Whatever Claude left behind competes too, so a killed run still ships its best state: it gets one real
    # check if nothing has checked it yet and nothing better is held.
    left = _final_script(dirs.submission / "script.sh") if facts.is_terminal else None
    if not facts.is_terminal:
        try:
            left = _final_patch(dirs, facts).diff
        except PatchBuildError:
            left = None
    held = keeper.best
    if left and keeper.verdict(left) is None and (held is None or held.rank < Rank.CHECKED):
        gate.final_check()
    if not facts.is_terminal:
        try:
            final = _final_patch(dirs, facts)
        except PatchBuildError as exc:  # the held answer still ships
            archive.write("final_patch.error.txt", f"{exc}\n")
            final = None
        if final is not None and final.ok and final.diff and keeper.verdict(final.diff) is None:
            keeper.offer(final.diff, Rank.STATIC_OK, "final state")
    else:
        script = _final_script(dirs.submission / "script.sh")
        if script is not None and keeper.verdict(script) is None:
            keeper.offer(script, Rank.STATIC_OK, "final state")

    best = keeper.best
    content, rank = (best.content, best.rank) if best else (b"", Rank.EMPTY)
    outcome = "stalled" if gate.state.stalled else (agent_result.ended if agent_result else "error")
    archive.write("submission.diff" if not facts.is_terminal else "script.sh", content)
    archive.write("gate.json", gate.state.history)
    for name in list_checks(dirs.checks):
        archive.write(f"checks/{name}", (dirs.checks / name).read_bytes())
    log = write_work_log(archive, pack, settings, clock, content, work_log)
    grade = reason = None
    if grader is not None:
        grade, reason = grader(pack, content)
        archive.write("grade.txt", f"{grade}\n{reason}\n")
    usage = gateway.usage if gateway is not None else None
    archive.summary(
        Path(settings.runs_dir) / "index.jsonl", task=pack.name, kind=facts.task_kind, language=facts.language,
        prompt_version=PROMPT_VERSION, model=settings.model, effort=settings.effort, outcome=outcome,
        grade=grade, reason=reason, gate_rounds=gate.state.rounds, seconds=round(time.monotonic() - started),
        budget_s=settings.task_budget_s, input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None), rate_limited=getattr(usage, "rate_limited", None),
        authorization=settings.anthropic_authorization or None, trajectory_bytes=log.get("bytes"),
        trajectory_level=log.get("level"), trajectory_ok=log.get("ok"),
    )
    return SolveResult(content, rank, outcome, gate.state.rounds, agent_result, grade, reason or "",
                       trajectory=log.get("data"), trajectory_error=log.get("error", ""))


def write_work_log(archive: RunArchive, pack: TaskPack, settings: Settings, clock: Clock, content: bytes,
                   spec: WorkLogSpec | None = None) -> dict:
    """Build ``trajectory.json`` from the recorded traffic. Runs after Claude stopped; never changes the answer.

    The log is never over the limit (``build_within`` asserts it) and any failure is reported, not raised.
    """

    if not settings.trajectory:
        return {}
    from honeminer.trajectory import LOCAL_MAX_BYTES, Header, build_from_traffic

    if spec is not None:
        header, max_bytes = spec.header, spec.max_bytes
    else:
        header = Header(task_id=pack.task_id, challenge_id=f"local-{archive.root.name}"[:128], miner_hotkey="local",
                        model_name=settings.model)
        max_bytes = settings.trajectory_max_bytes or LOCAL_MAX_BYTES
    try:
        built = build_from_traffic(archive.root / "traffic.jsonl", header, content, max_bytes,
                                   timeout_s=clock.work_log_timeout(settings.trajectory_build_s))
    except Exception as exc:  # noqa: BLE001 - the answer must never depend on the log
        archive.write("trajectory.error.txt", f"{type(exc).__name__}: {exc}\n")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    archive.write("trajectory.json", built.data)
    return {"ok": True, "bytes": len(built.data), "level": built.level, "data": built.data}


def _verify_timeout(clock: Clock, settings: Settings) -> int:
    return max(1, int(clock.call_timeout(settings.verify_facts_s)))


def _final_script(path: Path) -> bytes | None:
    """The script Claude left, if the grader would accept it at all (rlvr's validate_script)."""

    from rlvr.policy import RELEASE_POLICY
    from rlvr.v3.script import ScriptLimits, validate_script

    if not path.is_file():
        return None
    data = path.read_bytes()
    checked = validate_script(data, ScriptLimits(max_script_bytes=RELEASE_POLICY.v3_script_bytes))
    return None if checked.status == "rejected" else data


def _final_patch(dirs: TaskDirs, facts: Facts):
    return build_patch(dirs.baseline, dirs.work, protected=facts.protected)


def verify_facts(facts: Facts, baseline: Path, runner: GateRunner, scratch: Path, clock: Clock,
                 settings: Settings) -> tuple[Facts, bool]:
    """Run each command once on a copy of the original; drop the ones that cannot even start.

    Returns the facts to show Claude and whether the existing tests pass on the original
    (only then does the gate require them to keep passing). Each run gets HONEMINER_VERIFY_FACTS_S, never past
    the agent stop; a command that runs out of time is kept (its result is unknown, not "cannot start").
    """

    from dataclasses import replace

    from honeminer.selfcheck import _shell, _work_cwd

    cwd = _work_cwd(facts.working_directory)
    copy = scratch / "verify"
    shutil.copytree(baseline, copy, symlinks=True)
    build, tests_pass, test_cmd = facts.build_cmd, False, facts.test_cmd
    if build:
        result = runner.run(copy, _shell(build), cwd, timeout_s=_verify_timeout(clock, settings))
        if result.exit_code in (126, 127) and not result.timed_out:
            build = None
    if test_cmd:
        result = runner.run(copy, _shell(test_cmd), cwd, timeout_s=_verify_timeout(clock, settings))
        if result.exit_code in (126, 127) and not result.timed_out:
            test_cmd = None
        tests_pass = result.exit_code == 0 and not result.timed_out
    shutil.rmtree(copy, ignore_errors=True)
    return replace(facts, build_cmd=build, test_cmd=test_cmd), tests_pass


def solve_with_claude(pack: TaskPack, settings: Settings, *, clock: Clock | None = None,
                      work_log: WorkLogSpec | None = None, grade: bool = True) -> SolveResult:
    """The production path: real gateway, sandboxed Claude CLI, grading-identical gate containers.

    A live offer passes its own clock and work-log spec, and ``grade=False`` (an offer has no verifier).
    """

    import os

    from rlvr.policy import RELEASE_POLICY

    from honeminer.gateway import Gateway, GatewayConfig
    from honeminer.grade import grade_submission
    from honeminer.runner import ClaudeAgent
    from honeminer.sandbox import AgentSandbox, SandboxSpec, remove_orphans
    from honeminer.selfcheck import DockerGateRunner

    claude_bin = Path(settings.claude_bin or shutil.which("claude") or "").resolve()
    if not claude_bin.is_file():
        raise ConfigError("the native claude binary was not found; set HONEMINER_CLAUDE_BIN")
    credential = settings.api_key if settings.auth == "api_key" else settings.oauth_token
    if not credential:
        raise ConfigError(f"HONEMINER_AUTH={settings.auth} but its credential is empty "
                          "(run `claude setup-token` for CLAUDE_CODE_OAUTH_TOKEN, or set ANTHROPIC_API_KEY)")
    image = settings.image or RELEASE_POLICY.v3_image
    remove_orphans()
    runner = DockerGateRunner(image)
    archive = RunArchive.create(Path(settings.runs_dir), pack.name, secrets=(credential,))
    socket_dir = archive.root / "sock"
    socket_dir.mkdir()
    os.chmod(socket_dir, 0o755)

    def gateway_factory(gate, dirs):
        traffic = archive.root / "traffic.jsonl" if settings.trajectory else None
        gateway = Gateway(GatewayConfig(socket_dir / "gateway.sock", settings.model, settings.auth, credential,
                                        traffic=traffic), gate)
        gateway.start()
        return gateway

    def agent_factory(dirs, facts, clock):
        spec = SandboxSpec(image=image, workspace=dirs.work, checks=dirs.checks, kit=dirs.kit,
                           claude_md=dirs.kit / "CLAUDE.md", baseline=dirs.baseline, socket_dir=socket_dir,
                           claude_bin=claude_bin, cpus=settings.agent_cpus, memory_bytes=settings.agent_memory,
                           pids=settings.agent_pids, scratch_bytes=settings.agent_scratch,
                           submission=dirs.submission if facts.is_terminal else None)
        sandbox = AgentSandbox(spec)
        sandbox.start()
        return _StoppingAgent(ClaudeAgent(sandbox, settings, archive.root / "claude.stream.jsonl"), sandbox)

    def grader(task: TaskPack, content: bytes) -> tuple[str, str]:
        result = grade_submission(task, content, image=image)
        return result.status, result.reason_code or result.reason

    return solve(pack, settings, runner=runner, agent_factory=agent_factory, archive=archive, clock=clock,
                 gateway_factory=gateway_factory, grader=grader if grade else None, work_log=work_log)


class _StoppingAgent:
    def __init__(self, agent, sandbox) -> None:
        self.agent, self.sandbox = agent, sandbox

    def run(self, deadline_s: float) -> AgentResult:
        try:
            return self.agent.run(deadline_s)
        finally:
            self.sandbox.stop()
