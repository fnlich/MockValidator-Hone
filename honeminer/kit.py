"""The per-task kit: generated CLAUDE.md, prompt, hooks-only settings and hook scripts.

Layout inside the agent container (all read-only except /work and /task/checks):

    /task/CLAUDE.md        rules + verified commands (Claude Code loads it: parent of cwd /work)
    /task/.kit/            settings.json, kit.json, prompt.txt, hooks/, run_checks.py, mydiff.py
    /task/.base/           the pristine workspace, for mydiff.py
    /task/checks/          Claude's own checks (read-write)
    /work                  the workspace (read-write)

Every line of the rendered text must prevent a mistake or state a fact Claude
cannot find by reading the repository; the lint in tests enforces the budget.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from honeminer.facts import Facts

PROMPT_VERSION = "v3"
TEMPLATES = Path(__file__).resolve().parent / "kit_templates"
KIT_MOUNT = "/task/.kit"
GATE_URL = "http://127.0.0.1:8080/_honeminer/gate"
PROTECTED_GLOBS = (".rlvr/**", ".prebuilt/**")
EMPHASIS_WORDS = ("IMPORTANT", "MUST", "NEVER", "CRITICAL", "ALWAYS")


@dataclass(frozen=True)
class KitTiming:
    start_epoch: float
    agent_stop_epoch: float
    time_notices: tuple[float, ...]
    gate_timeout_s: int

    @property
    def minutes(self) -> int:
        return max(1, round((self.agent_stop_epoch - self.start_epoch) / 60))


def _protected_phrase(facts: Facts) -> str | None:
    parts = []
    if facts.protected:
        parts.append("Existing test files")
    if any(g.startswith(".rlvr") for g in PROTECTED_GLOBS):
        parts.append(".rlvr/")
    parts.append(".prebuilt/")
    return ", ".join(parts) if not facts.is_terminal else None


def render_claude_md(facts: Facts, timing: KitTiming) -> str:
    lines = ["# Grading"]
    if facts.is_terminal:
        lines += [
            "Hidden checks run /submission/script.sh with `bash --noprofile --norc` on a fresh copy of the",
            f"environment (cwd /work/{facts.result_tree_path}), then inspect the result and compare output byte-for-byte.",
            "All must pass.",
        ]
    else:
        lines += [
            "Hidden checks run commands on a clean copy with your diff applied and compare exit code, stdout and",
            "stderr byte-for-byte. All must pass; fixing one of two defects scores zero.",
        ]
    lines += ["", "# Commands (verified here)"]
    if facts.build_cmd:
        lines.append(f"- Build: `{facts.build_cmd}`")
    if facts.test_cmd:
        lines.append(f"- Tests: `{facts.test_cmd}`")
    if facts.driver_recipe:
        lines.append(f"- Driver: `{facts.driver_recipe}`")
    lines.append(f"- Your checks: `python3 {KIT_MOUNT}/run_checks.py`")
    if not facts.is_terminal:
        lines.append(f"- Your diff: `python3 {KIT_MOUNT}/mydiff.py` (no git history in /work)")
    lines += [
        "",
        "# Rules",
        "- You work alone; no one will answer questions. Decide and continue.",
        "- Put each check in /task/checks/NN-name.sh: exit 0 if correct, exit 1 and print expected vs actual if not.",
        "- Take expected values from the docs or task text, not from what the current code prints.",
        "- Change only what the fix needs, and leave no debug output: outputs are compared exactly.",
    ]
    protected = _protected_phrase(facts)
    if protected:
        lines.append(f"- {protected} are read-only.")
    lines += [f"- {gotcha}" for gotcha in facts.gotchas]
    if facts.is_terminal:
        lines.append("- When you stop, a gate runs your script on a fresh copy and requires each check to fail")
        lines.append(f"  before the script and pass after it. About {timing.minutes} minutes in total.")
    else:
        lines.append("- When you stop, a gate applies your diff to a clean copy, builds it, and requires each check")
        lines.append(f"  to fail on the original and pass on yours. About {timing.minutes} minutes in total.")
    return "\n".join(lines) + "\n"


BUGFIX_STEPS = (
    "1. List what the docs promise for the component the task names, including edge cases.",
    "2. Compare the implementation with each promise, and write a failing check for each mismatch.",
    "3. Fix the root causes. After each defect, search for the same pattern elsewhere; tasks usually have more "
    "than one.",
    "4. Build, run your checks and the tests, then stop.",
)
FEATURE_STEPS = (
    "1. The task text is the spec.",
    "2. Follow the conventions of the closest existing feature: names, messages, output format.",
    "3. Write failing checks for the new behavior, then implement it.",
    "4. Build, run your checks and the tests, then stop.",
)


def render_prompt(facts: Facts, instruction: str) -> str:
    head = f"<task>\n{instruction}\n</task>\n\n"
    if facts.is_terminal:
        tree = facts.result_tree_path
        body = (
            f"The environment is in /work. Write /submission/script.sh; the grader runs it with "
            f"`bash --noprofile --norc` on a fresh copy, cwd /work/{tree}, empty stdin, 300 s, no network. "
            "Test only on a fresh copy:\n"
            f"rm -rf /tmp/t && cp -a /work /tmp/t && cd /tmp/t/{tree} && "
            "bash --noprofile --norc /submission/script.sh </dev/null\n"
            "Make output deterministic (LC_ALL=C, sorted, no timestamps).\n"
        )
        return head + body
    docs = ", ".join(facts.doc_files) if facts.doc_files else "README and doc comments"
    intro = (
        f"The code is in /work ({facts.language}). Its documentation ({docs}) is the specification; "
        "where code and docs disagree, the docs are right.\n\n"
    )
    steps = FEATURE_STEPS if facts.task_kind == "feature" else BUGFIX_STEPS
    return head + intro + "\n".join(steps) + "\n"


def render_settings(timing: KitTiming) -> dict:
    def command(name: str) -> dict:
        return {"type": "command", "command": f"python3 {KIT_MOUNT}/hooks/{name}"}

    return {
        "hooks": {
            "SessionStart": [{"hooks": [command("session_start.py")]}],
            "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [command("protect.py")]}],
            "PostToolUse": [
                {"matcher": "Edit|Write|MultiEdit", "hooks": [command("check_edit.py")]},
                {"matcher": "*", "hooks": [command("time_notice.py")]},
            ],
            "Stop": [{"hooks": [dict(command("gate.py"), timeout=timing.gate_timeout_s)]}],
        }
    }


def write_kit(destination: Path, facts: Facts, instruction: str, timing: KitTiming) -> Path:
    """Write the kit directory (mounted at /task/.kit); CLAUDE.md sits beside it."""

    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(TEMPLATES / "hooks", destination / "hooks")
    for name in ("run_checks.py", "mydiff.py"):
        shutil.copy2(TEMPLATES / name, destination / name)
    (destination / "CLAUDE.md").write_text(render_claude_md(facts, timing))
    (destination / "prompt.txt").write_text(render_prompt(facts, instruction))
    (destination / "settings.json").write_text(json.dumps(render_settings(timing), indent=2) + "\n")
    (destination / "facts.json").write_text(facts.to_json())
    kit = {
        "prompt_version": PROMPT_VERSION,
        "start_epoch": timing.start_epoch,
        "agent_stop_epoch": timing.agent_stop_epoch,
        "time_notices": list(timing.time_notices),
        "gate_url": GATE_URL,
        "gate_timeout_s": timing.gate_timeout_s,
        "protected": list(facts.protected),
        "protected_globs": list(PROTECTED_GLOBS),
    }
    (destination / "kit.json").write_text(json.dumps(kit, indent=2) + "\n")
    return destination


def lint(claude_md: str, prompt: str, instruction: str) -> list[str]:
    """Problems with rendered text; an empty list means it is within budget."""

    problems = []
    for name, text in (("CLAUDE.md", claude_md), ("prompt", prompt.replace(instruction, ""))):
        leftover = re.findall(r"\{[a-z_]+\}", text)
        if leftover:
            problems.append(f"{name}: unfilled placeholder {leftover[0]}")
    if len(claude_md.splitlines()) > 25:
        problems.append(f"CLAUDE.md has {len(claude_md.splitlines())} lines (max 25)")
    body = prompt.replace(instruction, "")
    if len(body.splitlines()) > 12:
        problems.append(f"prompt has {len(body.splitlines())} lines besides the task (max 12)")
    emphasis = sum(word in EMPHASIS_WORDS for word in (claude_md + body).split())
    if emphasis > 1:
        problems.append(f"{emphasis} emphasis words (max 1)")
    shared = {line.strip() for line in claude_md.splitlines() if len(line.strip()) > 20} & {
        line.strip() for line in body.splitlines() if len(line.strip()) > 20
    }
    if shared:
        problems.append(f"line repeated in CLAUDE.md and prompt: {sorted(shared)[0]}")
    return problems
