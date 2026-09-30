"""SessionStart: leave a marker so the harness knows the settings (and hooks) loaded."""

import sys

with open("/tmp/.honeminer-hooks-loaded", "w", encoding="utf-8") as handle:
    handle.write("ok\n")
sys.exit(0)
