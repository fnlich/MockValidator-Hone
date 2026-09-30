"""Preflight checks, and the Phase 0 go/no-go spike.

``doctor`` checks the host. ``doctor --spike`` also runs the real Claude CLI
once in the sandbox, through the gateway with the injected credential, on a
tiny task: it proves the relay, the auth path, the hooks (the Stop hook must
reach the gate) and the Grep tool before any real task depends on them, and
that the work log built from the recorded traffic is valid.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from honeminer.config import Settings


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True

    def line(self) -> str:
        mark = "ok  " if self.ok else ("FAIL" if self.required else "warn")
        return f"[{mark}] {self.name}: {self.detail}"


def _run(argv: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def claude_binary(settings: Settings) -> Path | None:
    found = settings.claude_bin or shutil.which("claude")
    return Path(found).resolve() if found and Path(found).is_file() else None


def _is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) == b"\x7fELF"
    except OSError:
        return False


def host_checks(settings: Settings) -> list[Check]:
    from rlvr.policy import RELEASE_POLICY

    checks = [Check("platform", platform.system() == "Linux", platform.platform())]
    docker = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    checks.append(Check("docker", docker.returncode == 0, docker.stdout.strip() or docker.stderr.strip()[:200]))
    checks.append(Check("non-root user", os.getuid() != 0,
                        "rlvr refuses to grade as root" if os.getuid() == 0 else f"uid {os.getuid()}"))
    image = settings.image or RELEASE_POLICY.v3_image
    present = _run(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
    checks.append(Check("sandbox image", present.returncode == 0,
                        image if present.returncode == 0 else f"missing: docker pull {image}"))
    binary = claude_binary(settings)
    version = _run([str(binary), "--version"]) if binary else None
    with_elf = binary is not None and _is_elf(binary)
    checks.append(Check("claude binary", bool(binary) and with_elf and version.returncode == 0,
                        f"{binary} {version.stdout.strip() if version else ''}".strip() if binary
                        else "not found; install Claude Code natively or set HONEMINER_CLAUDE_BIN"))
    credential = settings.api_key if settings.auth == "api_key" else settings.oauth_token
    checks.append(Check("credential", bool(credential),
                        f"HONEMINER_AUTH={settings.auth}" + ("" if credential else " but the credential is empty")))
    runs = Path(settings.runs_dir).resolve()
    try:
        runs.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(runs).free / 1024**3
        checks.append(Check("disk", free_gb >= 20, f"{free_gb:.0f} GiB free for {runs}"))
    except OSError as exc:
        checks.append(Check("disk", False, f"runs directory {runs} is not usable: {exc}"))
    ntp = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    checks.append(Check("clock sync", ntp.stdout.strip() == "yes", ntp.stdout.strip() or "unknown", required=False))
    git = _run(["git", "--version"])
    checks.append(Check("git", git.returncode == 0, git.stdout.strip(), required=False))
    if settings.live:
        blockers = settings.live_blockers()
        checks.append(Check("live mode", not blockers, "; ".join(blockers) or settings.mode))
        try:
            import bittensor  # type: ignore[import-not-found]  # noqa: F401
            chain = (True, "bittensor importable")
        except ImportError:
            chain = (False, "missing: pip install -e '../hone-subnet[miner,chain]'")
        checks.append(Check("chain extras", *chain))
    return checks


SPIKE_PROMPT = (
    "This is a connectivity test. Use the Grep tool to find which file under /work contains the word "
    "'needle', write that file's name (relative to /work) into /work/found.txt, then stop."
)


def spike(settings: Settings, timeout_s: float = 300) -> list[Check]:
    """One real Claude CLI run in the sandbox; returns what worked."""

    from rlvr.policy import RELEASE_POLICY

    from honeminer.gateway import Gateway, GatewayConfig
    from honeminer.runner import ClaudeAgent
    from honeminer.sandbox import AgentSandbox, SandboxError, SandboxSpec, socket_dir

    binary = claude_binary(settings)
    credential = settings.api_key if settings.auth == "api_key" else settings.oauth_token
    if binary is None or not credential:
        return [Check("spike", False, "needs the claude binary and a credential")]
    gate_calls: list[dict] = []
    runs = Path(settings.runs_dir).resolve()
    try:
        runs.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return [Check("spike", False, f"runs directory {runs} is not usable: {exc}")]
    with tempfile.TemporaryDirectory(prefix="honeminer-spike-", dir=runs) as scratch, socket_dir() as sockets:
        root = Path(scratch)
        for name in ("work", "checks", "kit", "base"):
            (root / name).mkdir()
        (root / "work" / "notes.txt").write_text("nothing here\n")
        (root / "work" / "hay.txt").write_text("a needle in the haystack\n")
        from honeminer.facts import Facts
        from honeminer.kit import KitTiming, write_kit

        facts = Facts("repository_patch_v1", "bug_fix", "text", ".", ".", None, None, None, (), (), ())
        now = time.time()
        write_kit(root / "kit-out", facts, "connectivity test",
                  KitTiming(now, now + timeout_s, settings.time_notices, 60))
        for item in (root / "kit-out").iterdir():
            shutil.move(str(item), root / "kit" / item.name)
        (root / "kit" / "prompt.txt").write_text(SPIKE_PROMPT)
        gateway = Gateway(GatewayConfig(sockets / "gateway.sock", settings.model, settings.auth, credential,
                                        traffic=root / "traffic.jsonl"),
                          gate=lambda request: gate_calls.append(request) or {"decision": "allow"})
        try:
            gateway.start()
        except OSError as exc:
            return [Check("spike", False, f"the gateway could not start: {exc}")]
        spec = SandboxSpec(image=settings.image or RELEASE_POLICY.v3_image, workspace=root / "work",
                           checks=root / "checks", kit=root / "kit", claude_md=root / "kit" / "CLAUDE.md",
                           baseline=root / "base", socket_dir=sockets, claude_bin=binary)
        sandbox = AgentSandbox(spec)
        try:
            sandbox.start()
            result = ClaudeAgent(sandbox, settings, root / "stream.jsonl").run(timeout_s)
        except (SandboxError, OSError) as exc:
            return [Check("spike", False, str(exc))]
        finally:
            sandbox.stop()
            gateway.stop()
        found = (root / "work" / "found.txt")
        stream = (root / "stream.jsonl").read_text(errors="replace")[-2000:] if (root / "stream.jsonl").exists() else ""
        usage = gateway.usage
        work_log = _spike_work_log(root / "traffic.jsonl", settings,
                                   found.read_bytes() if found.is_file() else b"")
        return [
            Check("spike: claude ran unattended", result.ended == "finished" and result.exit_code == 0,
                  f"ended={result.ended} exit={result.exit_code}; tail: {stream[-300:]!r}"),
            Check("spike: model traffic through the gateway", usage.requests > 0 and not usage.rejected,
                  f"{usage.requests} requests, {usage.output_tokens} output tokens, rejected={usage.rejected}"),
            Check("spike: Grep tool + edit", found.is_file() and found.read_text().strip() == "hay.txt",
                  found.read_text().strip() if found.is_file() else "found.txt missing"),
            Check("spike: Stop hook reached the gate", bool(gate_calls), f"{len(gate_calls)} gate calls"),
            work_log,
        ]


def _spike_work_log(traffic: Path, settings: Settings, submission: bytes) -> Check:
    import hashlib

    from rlvr.v3.trajectory import parse_trajectory

    from honeminer.trajectory import LOCAL_MAX_BYTES, Header, build_from_traffic

    header = Header(task_id=hashlib.sha256(b"honeminer-spike").hexdigest(), challenge_id="local-spike",
                    miner_hotkey="local", model_name=settings.model)
    try:
        built = build_from_traffic(traffic, header, submission, settings.trajectory_max_bytes or LOCAL_MAX_BYTES,
                                   timeout_s=settings.trajectory_build_s)
        log = parse_trajectory(built.data)
    except Exception as exc:  # noqa: BLE001 - reported as a failed check
        return Check("spike: work log valid", False, f"{type(exc).__name__}: {exc}")
    turns = sum(event.event_type == "model_turn" for event in log.events)
    recorded = turns > 0 and built.level != "minimal-fallback" and traffic.is_file()
    return Check("spike: work log valid", recorded,
                 f"{len(log.events)} events, {turns} model turns, {len(built.data) / 1024:.1f} KB, level {built.level}")


def report(checks: list[Check]) -> tuple[str, bool]:
    ok = all(check.ok or not check.required for check in checks)
    return "\n".join(check.line() for check in checks), ok


def to_json(checks: list[Check]) -> str:
    return json.dumps([check.__dict__ for check in checks], indent=2)
