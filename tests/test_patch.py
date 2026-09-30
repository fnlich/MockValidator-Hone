import os
import subprocess

import pytest

from honeminer.patch import NEW_FILE_MAX_BYTES, build_patch
from honeminer.tasks import load_pack


@pytest.fixture(scope="module")
def yamlcpp(tmp_path_factory):
    pack = load_pack("cpp-yamlcpp")
    root = tmp_path_factory.mktemp("yamlcpp")
    (root / "s").mkdir()
    return pack, pack.extract("workspace", root / "base", root / "s")


def fresh_copy(pack, tmp_path, name):
    (tmp_path / f"s-{name}").mkdir()
    return pack.extract("workspace", tmp_path / name, tmp_path / f"s-{name}")


def git_apply_check(root, diff):
    return subprocess.run(["git", "apply", "--check", "-p1", "-"], cwd=root, input=diff, capture_output=True)


def test_build_cache_regression_on_the_real_task(yamlcpp, tmp_path):
    pack, base = yamlcpp
    work = fresh_copy(pack, tmp_path, "work")
    command = ["git", "apply", "-p1", "-"]
    applied = subprocess.run(command, cwd=work, input=pack.reference(), capture_output=True)
    assert applied.returncode == 0, applied.stderr
    # What running .rlvr/build.py leaves behind: rewritten tracked artifacts and caches.
    prebuilt = sorted(p for p in (work / ".prebuilt").rglob("*") if p.is_file())
    assert prebuilt, "the fixture ships a build cache"
    for path in prebuilt:
        path.write_bytes(path.read_bytes() + b"\x00rebuilt")
    (work / ".rlvr" / "__pycache__").mkdir(exist_ok=True)
    (work / ".rlvr" / "__pycache__" / "build.cpython-312.pyc").write_bytes(b"\x00\x01")
    (work / "a.out").write_bytes(b"\x7fELF\x00\x00")

    result = build_patch(base, work)
    assert result.ok, result.rejection
    assert sorted(result.changed) == ["src/eventarchive_query.cpp", "src/eventarchive_store.cpp"]
    assert b".prebuilt" not in result.diff and b"build-manifest" not in result.diff
    dropped = dict(result.dropped)
    assert all(str(p.relative_to(work)) in dropped for p in prebuilt)
    assert dropped["a.out"] == "binary file"
    assert git_apply_check(fresh_copy(pack, tmp_path, "check"), result.diff).returncode == 0


def test_unchanged_tree_gives_an_empty_patch(yamlcpp, tmp_path):
    pack, base = yamlcpp
    result = build_patch(base, fresh_copy(pack, tmp_path, "same"))
    assert result.diff == b"" and result.changed == () and result.ok


def make_tree(root, files):
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def test_new_deleted_and_filtered_files(tmp_path):
    base = make_tree(tmp_path / "base", {
        "src/a.py": b"A = 1\n", "src/old.py": b"OLD = 1\n", "tests/test_a.py": b"def test(): pass\n",
        ".gitignore": b"*.log\n",
    })
    work = make_tree(tmp_path / "work", {
        "src/a.py": b"A = 2\n", "tests/test_a.py": b"def test(): assert False\n", ".gitignore": b"*.log\n",
        "src/new.py": b"NEW = 1\n", "notes.log": b"ignored by .gitignore but still a change\n",
        "big.txt": b"x" * (NEW_FILE_MAX_BYTES + 1), "latin.txt": "café".encode("latin-1"),
        "src/__pycache__/a.cpython-312.pyc": b"cache",
    })
    os.symlink("src/a.py", work / "link.py")

    result = build_patch(base, work, protected=["tests/test_a.py"])
    dropped = dict(result.dropped)
    assert set(result.changed) == {"src/a.py", "src/new.py", "src/old.py", "notes.log"}
    assert dropped["tests/test_a.py"].startswith("protected")
    assert dropped["big.txt"].startswith("new file over")
    assert dropped["latin.txt"] == "not UTF-8 text"
    assert dropped["link.py"].startswith("symlink")
    assert dropped["src/__pycache__/a.cpython-312.pyc"] == "build output or cache"
    assert b"deleted file mode 100644" in result.diff and b"+NEW = 1" in result.diff
    assert result.ok
    assert git_apply_check(make_tree(tmp_path / "check", {
        "src/a.py": b"A = 1\n", "src/old.py": b"OLD = 1\n", "tests/test_a.py": b"def test(): pass\n",
        ".gitignore": b"*.log\n",
    }), result.diff).returncode == 0


def test_build_owned_paths_stay_at_baseline(tmp_path):
    base = make_tree(tmp_path / "base", {"gen/version.txt": b"1\n", "src/x.c": b"int x;\n"})
    work = make_tree(tmp_path / "work", {"gen/version.txt": b"2\n", "src/x.c": b"int x = 1;\n"})
    result = build_patch(base, work, build_owned={"gen/version.txt"})
    assert result.changed == ("src/x.c",)
    assert dict(result.dropped) == {"gen/version.txt": "rewritten by the task's build"}


def test_crlf_and_trailing_bytes_survive_exactly(tmp_path):
    base = make_tree(tmp_path / "base", {"a.txt": b"one\r\ntwo\r\n"})
    work = make_tree(tmp_path / "work", {"a.txt": b"one\r\nTWO\r\n"})
    result = build_patch(base, work)
    check = make_tree(tmp_path / "check", {"a.txt": b"one\r\ntwo\r\n"})
    subprocess.run(["git", "apply", "-p1", "-"], cwd=check, input=result.diff, check=True)
    assert (check / "a.txt").read_bytes() == b"one\r\nTWO\r\n"


@pytest.mark.parametrize("committed", [False, True], ids=["empty-nested-repo", "committed-nested-repo"])
def test_files_inside_a_nested_git_repo_are_still_captured(tmp_path, committed):
    base = make_tree(tmp_path / "base", {"src/a.py": b"A = 1\n"})
    work = make_tree(tmp_path / "work", {"src/a.py": b"A = 2\n", "tool/x.py": b"X = 1\n"})
    subprocess.run(["git", "init", "-q", str(work / "tool")], check=True)  # e.g. `cargo new tool`
    if committed:
        env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b", "GIT_COMMITTER_NAME": "a",
               "GIT_COMMITTER_EMAIL": "a@b"}
        subprocess.run(["git", "-C", str(work / "tool"), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(work / "tool"), "commit", "-qm", "x"], check=True, env=env)
    result = build_patch(base, work)
    assert set(result.changed) == {"src/a.py", "tool/x.py"} and result.ok
    assert b".git/" not in result.diff and b"+X = 1" in result.diff


def test_deleting_a_binary_or_non_utf8_file_is_dropped_not_fatal(tmp_path):
    base = make_tree(tmp_path / "base", {"src/a.py": b"A = 1\n", "blob.bin": b"\x00\x01\x02",
                                         "latin.txt": "café".encode("latin-1")})
    work = make_tree(tmp_path / "work", {"src/a.py": b"A = 2\n"})
    result = build_patch(base, work)
    assert result.ok, result.rejection
    assert result.changed == ("src/a.py",)
    assert set(dict(result.dropped)) == {"blob.bin", "latin.txt"}


def test_tracked_files_under_build_like_dirs_can_be_edited(tmp_path):
    base = make_tree(tmp_path / "base", {"build/config.js": b"a = 1\n", "pkg/target/x.go": b"package x\n"})
    work = make_tree(tmp_path / "work", {"build/config.js": b"a = 2\n", "pkg/target/x.go": b"package x // y\n",
                                         "build/out.js": b"generated\n"})
    result = build_patch(base, work)
    assert set(result.changed) == {"build/config.js", "pkg/target/x.go"}
    assert dict(result.dropped) == {"build/out.js": "build output or cache"}


def test_protected_paths_with_glob_characters_are_protected(tmp_path):
    base = make_tree(tmp_path / "base", {"tests/data[1].txt": b"1\n"})
    work = make_tree(tmp_path / "work", {"tests/data[1].txt": b"2\n"})
    result = build_patch(base, work, protected=["tests/data[1].txt"])
    assert result.changed == () and "tests/data[1].txt" in dict(result.dropped)
