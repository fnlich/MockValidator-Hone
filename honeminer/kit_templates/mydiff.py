"""Show your current changes against the original workspace (no git history in /work)."""

import subprocess
import sys

IGNORE = (".prebuilt/", ".rlvr/", "__pycache__/")
result = subprocess.run(
    ["git", "-c", "core.quotepath=off", "diff", "--no-index", "--no-color", "--no-renames",
     "/task/.base", "/work"],
    capture_output=True, text=True, errors="replace",
)
keep, skip = [], False
for line in result.stdout.splitlines():
    if line.startswith("diff --git "):
        skip = any(marker in line for marker in IGNORE)
    if not skip:
        keep.append(line.replace("a/task/.base/", "a/").replace("b/work/", "b/"))
print("\n".join(keep) if keep else "no changes yet")
sys.exit(0)
