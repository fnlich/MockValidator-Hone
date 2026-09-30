"""Shared helpers for the in-sandbox hooks (stdlib only; the image has no jq/curl)."""

import json
import os
import sys

KIT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = "/work"


def kit():
    with open(os.path.join(KIT_DIR, "kit.json"), encoding="utf-8") as handle:
        return json.load(handle)


def hook_input():
    try:
        return json.load(sys.stdin)
    except ValueError:
        return {}


def relative_to_work(path):
    if not path:
        return None
    absolute = os.path.normpath(path if os.path.isabs(path) else os.path.join(os.getcwd(), path))
    if absolute == WORK or not absolute.startswith(WORK + "/"):
        return None
    return absolute[len(WORK) + 1:]
