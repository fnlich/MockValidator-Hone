from pathlib import Path

from honeminer.sandbox import LABEL, OWNER_LABEL, SandboxSpec, exec_argv, forwarder_argv, remove_orphans, run_argv


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


def test_terminal_tasks_get_a_writable_submission_mount(tmp_path):
    (tmp_path / "submission").mkdir()
    argv = run_argv(SandboxSpec(**{**spec(tmp_path).__dict__, "submission": tmp_path / "submission"}))
    mount = next(v for v in argv if "target=/submission" in v)
    assert "readonly" not in mount and str(tmp_path / "submission") in mount
    assert not any("target=/submission" in v for v in run_argv(spec(tmp_path)))  # repository tasks: none


def test_each_container_is_labelled_with_its_owner_process(tmp_path):
    import os

    argv = run_argv(spec(tmp_path))
    assert f"{OWNER_LABEL}={os.getpid()}" in argv


def test_orphan_cleanup_spares_containers_of_live_processes(tmp_path):
    import os
    import subprocess
    import sys

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    log = tmp_path / "docker.log"
    fake = tmp_path / "docker"
    fake.write_text(f"""#!/bin/sh
echo "$@" >> {log}
if [ "$1" = ps ]; then
  echo "c-live {os.getpid()}"
  echo "c-dead {dead.pid}"
  echo "c-old "
fi
""")
    fake.chmod(0o755)
    assert remove_orphans(str(fake)) == 2
    removed = [line for line in log.read_text().splitlines() if line.startswith("rm")]
    assert removed == ["rm -f c-dead c-old"]


def test_gateway_sockets_live_in_a_short_private_directory():
    import os

    from honeminer.sandbox import socket_dir

    with socket_dir() as path:
        assert len(str(path / "gateway.sock")) < 100 and path.is_dir()  # AF_UNIX paths max out at 107 bytes
        assert oct(os.stat(path).st_mode & 0o777) == "0o755"
    assert not path.exists()
