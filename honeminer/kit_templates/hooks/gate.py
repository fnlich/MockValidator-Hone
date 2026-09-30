"""Stop hook: ask the host gate whether the work is finished; block with its reason if not."""

import json
import os
import sys
import urllib.error
import urllib.request

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _kit import hook_input, kit  # noqa: E402


def main():
    data = hook_input()
    config = kit()
    body = json.dumps({"session_id": data.get("session_id", ""),
                       "stop_hook_active": bool(data.get("stop_hook_active"))}).encode()
    headers = {"Content-Type": "application/json"}
    request = urllib.request.Request(config["gate_url"], data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=config.get("gate_timeout_s", 600)) as response:
            verdict = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        # Never trap Claude when the gate itself is unreachable; the harness still checks the result.
        print(json.dumps({"systemMessage": f"gate unreachable: {exc}"}))
        return 0
    if verdict.get("decision") == "block":
        print(json.dumps({"decision": "block", "reason": verdict.get("reason", "The gate did not pass.")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
