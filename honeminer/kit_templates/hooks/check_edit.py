"""PostToolUse (Edit/Write): syntax-check the edited file; errors go back to Claude."""

import ast
import json
import os
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _kit import hook_input  # noqa: E402


def problems(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".py":
            with open(path, "rb") as handle:
                ast.parse(handle.read(), path)
        elif ext == ".json":
            with open(path, encoding="utf-8") as handle:
                json.load(handle)
        else:
            command = {".js": ["node", "--check"], ".mjs": ["node", "--check"], ".cjs": ["node", "--check"],
                       ".sh": ["bash", "-n"], ".go": ["gofmt", "-e", "-l"]}.get(ext)
            if command is None:
                return None
            result = subprocess.run([*command, path], capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                return (result.stderr or result.stdout).strip()
    except (SyntaxError, ValueError) as exc:
        return f"{path}: {exc}"
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None


def main():
    data = hook_input()
    path = (data.get("tool_input") or {}).get("file_path") or ""
    found = problems(path) if path else None
    if found:
        print(f"Syntax check failed after your edit:\n{found[-3000:]}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
