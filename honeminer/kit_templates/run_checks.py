"""Run every NN-*.sh check the way the gate does (same cwd and per-check time limit); exit 0 only if all pass."""

import glob
import json
import os
import re
import subprocess
import sys

sys.dont_write_bytecode = True
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "kit.json"), encoding="utf-8") as handle:
    KIT = json.load(handle)
CHECKS_DIR = KIT.get("checks_dir", "/task/checks")
CWD = KIT.get("check_cwd", "/work")
TIMEOUT_S = KIT.get("check_timeout_s", 300)

checks = sorted(p for p in glob.glob(os.path.join(CHECKS_DIR, "*.sh")) if re.match(r"^\d\d-", os.path.basename(p)))
if not checks:
    print(f"no checks yet: add {CHECKS_DIR}/NN-name.sh")
    sys.exit(1)
failed = 0
for path in checks:
    name = os.path.basename(path)
    try:
        result = subprocess.run(["bash", path], cwd=CWD, capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT  {name} (over {TIMEOUT_S} s; the gate would stop it too)")
        failed += 1
        continue
    status = {0: "PASS", 1: "FAIL"}.get(result.returncode, f"INVALID (exit {result.returncode})")
    print(f"{status}  {name}")
    if result.returncode != 0:
        failed += 1
        tail = (result.stdout + result.stderr).strip().splitlines()[-15:]
        print("\n".join("    " + line for line in tail))
sys.exit(1 if failed else 0)
