"""The agent container in the real image (CI docker job)."""

import shutil
import time
from pathlib import Path

import pytest

from honeminer.config import load_env
from honeminer.gateway import Gateway, GatewayConfig
from honeminer.sandbox import AgentSandbox, SandboxSpec

pytestmark = pytest.mark.docker


@pytest.fixture
def sandbox(tmp_path):
    from rlvr.policy import RELEASE_POLICY

    for name in ("w", "c", "k", "b", "sock"):
        (tmp_path / name).mkdir()
    (tmp_path / "w" / "main.py").write_text("print('hi')\n")
    (tmp_path / "w" / ".claude").mkdir()
    (tmp_path / "w" / ".claude" / "settings.json").write_text('{"hooks": {}}')
    (tmp_path / "CLAUDE.md").write_text("# kit\n")
    shutil.copy(Path(__file__).resolve().parent.parent / "honeminer" / "kit_templates" / "forwarder.py",
                tmp_path / "k" / "forwarder.py")
    fake_claude = tmp_path / "claude"
    fake_claude.write_text("#!/bin/sh\necho fake-claude\n")
    fake_claude.chmod(0o755)
    gateway = Gateway(GatewayConfig(tmp_path / "sock" / "gateway.sock", "m", "api_key", "k"),
                      gate=lambda request: {"decision": "allow", "echo": request})
    gateway.start()
    spec = SandboxSpec(image=load_env().image or RELEASE_POLICY.v3_image, workspace=tmp_path / "w",
                       checks=tmp_path / "c", kit=tmp_path / "k", claude_md=tmp_path / "CLAUDE.md",
                       baseline=tmp_path / "b", socket_dir=tmp_path / "sock", claude_bin=fake_claude)
    box = AgentSandbox(spec)
    box.start()
    time.sleep(1)
    yield box
    box.stop()
    gateway.stop()


GATE_CALL = ("import json, urllib.request; r = urllib.request.urlopen(urllib.request.Request("
             "'http://127.0.0.1:8080/_honeminer/gate', data=b'{\"ping\": 1}', "
             "headers={'Content-Type': 'application/json'}), timeout=10); print(json.loads(r.read())['echo'])")


def test_agent_has_no_network_but_reaches_the_gateway(sandbox):
    outside = sandbox.exec(["python3", "-c", "import socket; socket.create_connection(('1.1.1.1', 443), 3)"])
    assert outside.returncode != 0
    gate = sandbox.exec(["python3", "-c", GATE_CALL])
    assert gate.returncode == 0 and "'ping': 1" in gate.stdout


def test_agent_is_not_root_and_cannot_stop_the_forwarder(sandbox):
    assert sandbox.exec(["id", "-u"]).stdout.strip() != "0"
    kill = ("import os, signal\n"
            "for pid in filter(str.isdigit, os.listdir('/proc')):\n"
            "    if int(pid) == os.getpid():\n"
            "        continue\n"
            "    try:\n"
            "        if b'/task/.kit/forwarder.py' in open(f'/proc/{pid}/cmdline', 'rb').read().split(b'\\0'):\n"
            "            os.kill(int(pid), signal.SIGKILL)\n"
            "    except OSError as exc:\n"
            "        print('refused', exc)\n")
    assert "refused" in sandbox.exec(["python3", "-c", kill]).stdout
    assert sandbox.exec(["python3", "-c", GATE_CALL]).returncode == 0


def test_workspace_is_writable_and_repo_claude_config_is_hidden(sandbox):
    assert sandbox.exec(["sh", "-c", "echo x > /work/new.txt"]).returncode == 0
    assert sandbox.exec(["ls", "-A", "/work/.claude"]).stdout.strip() == ""
    assert sandbox.exec(["sh", "-c", "echo x > /task/.kit/x"]).returncode != 0
    assert sandbox.exec(["/opt/claude/claude"]).stdout.strip() == "fake-claude"
