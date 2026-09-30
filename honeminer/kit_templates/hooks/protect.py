"""PreToolUse (Edit/Write): protected files are read-only; new files are fine."""

import fnmatch
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _kit import hook_input, kit, relative_to_work  # noqa: E402


def main():
    data = hook_input()
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    config = kit()
    if os.path.abspath(path).startswith(("/task/.kit", "/task/CLAUDE.md", "/task/.base")):
        print("Blocked: task kit files are read-only.", file=sys.stderr)
        return 2
    relative = relative_to_work(path)
    if relative is None:
        return 0
    protected = set(config.get("protected", []))
    globs = config.get("protected_globs", [])
    if relative in protected or any(fnmatch.fnmatchcase(relative, p) for p in globs):
        print(f"Blocked: {relative} is read-only (task build files and existing tests). "
              "Fix the source instead; put new checks in /task/checks/.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
