"""PostToolUse (all tools): one factual line when 50/25/10% of the time is left."""

import json
import os
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _kit import kit  # noqa: E402

STATE = "/tmp/.honeminer-time-notices"


def main():
    config = kit()
    start, stop = config["start_epoch"], config["agent_stop_epoch"]
    total = max(1.0, stop - start)
    left = max(0.0, stop - time.time())
    fraction = left / total
    try:
        with open(STATE, encoding="utf-8") as handle:
            given = set(json.load(handle))
    except (OSError, ValueError):
        given = set()
    due = [f for f in config.get("time_notices", []) if fraction <= f and str(f) not in given]
    if not due:
        return 0
    given.update(str(f) for f in due)
    with open(STATE, "w", encoding="utf-8") as handle:
        json.dump(sorted(given), handle)
    minutes = max(1, round(left / 60))
    output = {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                     "additionalContext": f"About {minutes} minutes left."}}
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
