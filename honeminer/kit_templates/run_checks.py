"""Run every /task/checks/NN-*.sh; exit 0 only if all pass."""

import glob
import re
import subprocess
import sys

checks = sorted(p for p in glob.glob("/task/checks/*.sh") if re.match(r"^\d\d-", p.rsplit("/", 1)[1]))
if not checks:
    print("no checks yet: add /task/checks/NN-name.sh")
    sys.exit(1)
failed = 0
for path in checks:
    result = subprocess.run(["bash", path], cwd="/work", capture_output=True, text=True, timeout=600)
    status = {0: "PASS", 1: "FAIL"}.get(result.returncode, f"INVALID (exit {result.returncode})")
    print(f"{status}  {path.rsplit('/', 1)[1]}")
    if result.returncode != 0:
        failed += 1
        tail = (result.stdout + result.stderr).strip().splitlines()[-15:]
        print("\n".join("    " + line for line in tail))
sys.exit(1 if failed else 0)
