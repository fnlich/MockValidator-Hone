"""The agent container: where Claude CLI works on a task.

It runs the grading image with no network, as the host user's uid:gid (bypass
mode refuses root, and the extracted workspace belongs to that user). Its only
way out is the forwarder (started as root, so the agent cannot stop it) to the
host gateway's unix socket. Gate checks do not run here: they use fresh,
grading-identical containers through rlvr's own supervisor.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from honeminer.facts import SANDBOX_ENV

LABEL = "honeminer.role"
FORWARD_PORT = 8080
SOCKET_DIR = "/run/honeminer"


class SandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class SandboxSpec:
    image: str
    workspace: Path  # -> /work (rw)
    checks: Path  # -> /task/checks (rw)
    kit: Path  # -> /task/.kit (ro)
    claude_md: Path  # -> /task/CLAUDE.md (ro)
    baseline: Path  # -> /task/.base (ro)
    socket_dir: Path  # -> /run/honeminer (ro; holds the gateway socket)
    claude_bin: Path  # -> /opt/claude/claude (ro)
    cpus: int = 4
    memory_bytes: int = 8 * 1024**3
    pids: int = 4096
    scratch_bytes: int = 8 * 1024**3
    user: str = field(default_factory=lambda: f"{os.getuid()}:{os.getgid()}")
    name: str = field(default_factory=lambda: f"honeminer-agent-{uuid.uuid4().hex[:12]}")
    extra_env: tuple[tuple[str, str], ...] = ()


def run_argv(spec: SandboxSpec) -> list[str]:
    """``docker run`` for a long-lived agent container (commands arrive via exec)."""

    mounts = [
        (spec.workspace, "/work", False),
        (spec.checks, "/task/checks", False),
        (spec.kit, "/task/.kit", True),
        (spec.claude_md, "/task/CLAUDE.md", True),
        (spec.baseline, "/task/.base", True),
        (spec.socket_dir, SOCKET_DIR, True),
        (spec.claude_bin, "/opt/claude/claude", True),
    ]
    argv = [
        "docker", "run", "-d", "--rm", "--name", spec.name, "--label", f"{LABEL}=agent",
        "--network", "none", "--user", spec.user, "--read-only",
        "--tmpfs", f"/tmp:rw,exec,nosuid,size={spec.scratch_bytes}",
        "--cpus", str(spec.cpus), "--memory", str(spec.memory_bytes), "--memory-swap", str(spec.memory_bytes),
        "--pids-limit", str(spec.pids), "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
        "--workdir", "/work",
    ]
    for source, target, read_only in mounts:
        value = f"type=bind,source={Path(source).resolve()},target={target}"
        argv += ["--mount", value + (",readonly" if read_only else "")]
    # Repository-provided Claude Code settings, skills and agents must not reach the run.
    argv += ["--mount", "type=tmpfs,target=/work/.claude,tmpfs-mode=0555"]
    for key, value in (*SANDBOX_ENV.items(), *spec.extra_env):
        argv += ["--env", f"{key}={value}"]
    return [*argv, spec.image, "sleep", "infinity"]


def forwarder_argv(name: str) -> list[str]:
    return ["docker", "exec", "-d", "-u", "0", name, "python3", "/task/.kit/forwarder.py",
            str(FORWARD_PORT), f"{SOCKET_DIR}/gateway.sock"]


def exec_argv(name: str, argv: list[str], env: dict[str, str] | None = None, workdir: str = "/work") -> list[str]:
    command = ["docker", "exec", "-i", "--workdir", workdir]
    for key, value in (env or {}).items():
        command += ["--env", f"{key}={value}"]
    return [*command, name, *argv]


class AgentSandbox:
    def __init__(self, spec: SandboxSpec, docker: str | None = None) -> None:
        self.spec = spec
        self.docker = docker or shutil.which("docker") or "docker"

    def _run(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        argv = [self.docker, *argv[1:]]
        return subprocess.run(argv, capture_output=True, text=True, **kwargs)

    def start(self) -> None:
        started = self._run(run_argv(self.spec))
        if started.returncode != 0:
            raise SandboxError(f"agent container did not start: {started.stderr.strip()}")
        forwarder = self._run(forwarder_argv(self.spec.name))
        if forwarder.returncode != 0:
            self.stop()
            raise SandboxError(f"forwarder did not start: {forwarder.stderr.strip()}")

    def popen(self, argv: list[str], env: dict[str, str], stdout, stderr) -> subprocess.Popen:
        command = exec_argv(self.spec.name, argv, env)
        return subprocess.Popen([self.docker, *command[1:]], stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)

    def exec(self, argv: list[str], env: dict[str, str] | None = None, timeout: float = 120,
             user: str | None = None) -> subprocess.CompletedProcess:
        command = exec_argv(self.spec.name, argv, env)
        if user is not None:
            command[2:2] = ["-u", user]
        return self._run(command, timeout=timeout)

    def stop(self) -> None:
        """Stopping the container ends every process in it; state lives on host mounts."""

        self._run(["docker", "rm", "-f", self.spec.name])


def remove_orphans(docker: str | None = None) -> int:
    """Remove agent containers left over by a crashed run; returns how many."""

    binary = docker or shutil.which("docker") or "docker"
    listed = subprocess.run([binary, "ps", "-aq", "--filter", f"label={LABEL}"], capture_output=True, text=True)
    ids = listed.stdout.split()
    if ids:
        subprocess.run([binary, "rm", "-f", *ids], capture_output=True)
    return len(ids)
