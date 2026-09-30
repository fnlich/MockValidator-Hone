"""Stop hook: the turn may not end while lint or the fast tests fail.

Runs only when the working tree has changes, so answering a question does not
trigger the suite. After three consecutive blocks it lets the turn end with a
note, so a hopeless case cannot loop forever.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

MAX_BLOCKS = 3


def main() -> int:
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2]))
    state = root / ".claude" / ".stop_hook_blocks"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True)
    if not status.stdout.strip():
        state.unlink(missing_ok=True)
        return 0
    probe = subprocess.run([sys.executable, "-c", "import rlvr, honeminer"], cwd=root, capture_output=True)
    if probe.returncode != 0:
        print("stop hook: environment not set up (rlvr/honeminer not importable); skipped", file=sys.stderr)
        return 0
    checks = [
        ["ruff", "check", "honeminer", "tests"],
        [sys.executable, "-m", "pytest", "-q", "-x", "-m", "not docker and not live"],
    ]
    for command in checks:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True)
        if result.returncode != 0:
            blocks = int(state.read_text()) + 1 if state.exists() else 1
            if blocks > MAX_BLOCKS:
                state.unlink(missing_ok=True)
                print(json.dumps({"systemMessage": f"stop hook gave up: {' '.join(command)} still failing"}))
                return 0
            state.write_text(str(blocks))
            tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-40:])
            print(f"`{' '.join(command[-6:])}` failed; fix before finishing:\n{tail}", file=sys.stderr)
            return 2
    state.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
