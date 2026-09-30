"""Run Claude CLI unattended inside the agent container.

The agent is anything with ``run(deadline_s) -> AgentResult``; production uses
``ClaudeAgent`` (the real CLI in the sandbox), tests use a scripted fake that
edits files and calls the gate the way the Stop hook does.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from honeminer.config import Settings
from honeminer.sandbox import FORWARD_PORT, AgentSandbox

CLAUDE_IN_CONTAINER = "/opt/claude/claude"
CONFIG_DIR = "/tmp/claude-config"


@dataclass(frozen=True)
class AgentResult:
    exit_code: int | None
    ended: str  # finished | deadline | error
    seconds: float
    stream_path: Path | None = None
    error: str = ""


class Agent(Protocol):
    def run(self, deadline_s: float) -> AgentResult: ...


def claude_argv(settings: Settings, *, max_budget_usd: float | None = None) -> list[str]:
    argv = [
        CLAUDE_IN_CONTAINER, "-p",
        "--settings", "/task/.kit/settings.json",
        "--setting-sources", "user",
        "--strict-mcp-config",
        "--system-prompt-snapshot", "on",
        "--permission-mode", "bypassPermissions",
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence",
        "--model", settings.model,
        "--effort", settings.effort,
        "--disallowedTools", "WebSearch,WebFetch,Agent",
    ]
    if max_budget_usd is not None and settings.auth == "api_key":
        argv += ["--max-budget-usd", str(max_budget_usd)]
    return argv


def claude_env(settings: Settings) -> dict[str, str]:
    """Environment for the CLI: dummy credential (the gateway injects the real one), one model."""

    env = {
        "HOME": "/tmp/home",
        "CLAUDE_CONFIG_DIR": CONFIG_DIR,
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{FORWARD_PORT}",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": str(settings.stop_hook_block_cap),
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
    }
    for name in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                 "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
        env[name] = settings.model
    if settings.auth == "api_key":
        env["ANTHROPIC_API_KEY"] = "sandbox-placeholder"
    else:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = "sandbox-placeholder"
    return env


class ClaudeAgent:
    def __init__(self, sandbox: AgentSandbox, settings: Settings, stream_path: Path) -> None:
        self.sandbox = sandbox
        self.settings = settings
        self.stream_path = stream_path

    def _prepare(self) -> None:
        seed = json.dumps({"hasCompletedOnboarding": True, "bypassPermissionsModeAccepted": True})
        script = f"mkdir -p /tmp/home {CONFIG_DIR} && printf '%s' '{seed}' > {CONFIG_DIR}/.claude.json"
        self.sandbox.exec(["sh", "-c", script])

    def run(self, deadline_s: float) -> AgentResult:
        started = time.monotonic()
        self._prepare()
        argv = claude_argv(self.settings, max_budget_usd=self.settings.max_budget_usd)
        shell = " ".join(f"'{part}'" for part in argv) + " < /task/.kit/prompt.txt"
        with self.stream_path.open("wb") as out:
            process = self.sandbox.popen(["sh", "-c", shell], claude_env(self.settings), out, subprocess.STDOUT)
            try:
                code = process.wait(timeout=max(1.0, deadline_s))
                ended = "finished"
            except subprocess.TimeoutExpired:
                self.sandbox.stop()  # ends every process in the container; state is on host mounts
                process.wait(timeout=30)
                code, ended = None, "deadline"
        return AgentResult(code, ended, time.monotonic() - started, self.stream_path)
