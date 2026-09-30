from pathlib import Path

from honeminer.sandbox import LABEL, SandboxSpec, exec_argv, forwarder_argv, run_argv


def spec(tmp_path):
    return SandboxSpec(image="img@sha256:" + "0" * 64, workspace=tmp_path / "w", checks=tmp_path / "c",
                       kit=tmp_path / "k", claude_md=tmp_path / "CLAUDE.md", baseline=tmp_path / "b",
                       socket_dir=tmp_path / "sock", claude_bin=Path("/opt/claude-code/bin/claude"),
                       user="1000:1000", name="honeminer-agent-test")


def test_agent_container_is_isolated(tmp_path):
    argv = run_argv(spec(tmp_path))
    joined = " ".join(argv)
    assert argv[:3] == ["docker", "run", "-d"]
    assert "--network none" in joined and "--read-only" in joined and "--cap-drop ALL" in joined
    assert "--user 1000:1000" in joined and f"{LABEL}=agent" in joined
    assert f"source={(tmp_path / 'k').resolve()},target=/task/.kit,readonly" in joined
    assert f"source={(tmp_path / 'w').resolve()},target=/work" in joined
    assert "target=/work,readonly" not in joined
    assert "type=tmpfs,target=/work/.claude" in joined
    assert "--env PYTHONDONTWRITEBYTECODE=1" in joined and "--env GOPROXY=off" in joined
    assert argv[-3:] == ["img@sha256:" + "0" * 64, "sleep", "infinity"]


def test_forwarder_runs_as_root_and_exec_as_the_agent():
    assert forwarder_argv("n")[:6] == ["docker", "exec", "-d", "-u", "0", "n"]
    command = exec_argv("n", ["claude", "-p", "x"], {"A": "1"})
    assert "-u" not in command and command[-4:] == ["n", "claude", "-p", "x"] and "A=1" in command
