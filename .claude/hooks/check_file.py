"""PostToolUse hook: lint and syntax-check the file Claude just edited.

Exit 2 sends the errors back to Claude; exit 0 stays silent.
"""

import json
import shutil
import subprocess
import sys


def main() -> int:
    try:
        path = json.load(sys.stdin).get("tool_input", {}).get("file_path") or ""
    except ValueError:
        return 0
    problems = []
    if path.endswith(".py"):
        try:
            with open(path, encoding="utf-8") as handle:
                compile(handle.read(), path, "exec")
        except (OSError, SyntaxError, ValueError) as exc:
            problems.append(f"{path}: {exc}")
        ruff = shutil.which("ruff")
        if ruff and not problems:
            result = subprocess.run([ruff, "check", "--quiet", path], capture_output=True, text=True)
            if result.returncode != 0:
                problems.append((result.stdout + result.stderr).strip())
    elif path.endswith(".json"):
        try:
            with open(path, encoding="utf-8") as handle:
                json.load(handle)
        except (OSError, ValueError) as exc:
            problems.append(f"{path}: {exc}")
    elif path.endswith(".sh"):
        result = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        if result.returncode != 0:
            problems.append(result.stderr.strip())
    if problems:
        print("\n".join(problems)[-4000:], file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
