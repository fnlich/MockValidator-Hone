import json
import shutil

import pytest

from honeminer.tasks import PackError, list_packs, load_pack

YAMLCPP = "cpp-yamlcpp"


def test_real_fixture_loads_and_matches_its_task_id():
    pack = load_pack(YAMLCPP)
    assert pack.task_id == "e6225d044a252b86849bb14dc2f42ee399351dd3e9ded900712e4cdf1d6fe5c3"
    assert not pack.is_terminal and pack.language == "cpp"
    assert pack.reference().startswith(b"--- a/src/eventarchive_query.cpp")
    assert pack.root in list_packs()


def test_workspace_extracts_with_digest_check(tmp_path):
    pack = load_pack(YAMLCPP)
    (tmp_path / "scratch").mkdir()
    work = pack.extract("workspace", tmp_path / "work", tmp_path / "scratch")
    assert (work / "src" / "eventarchive_store.cpp").is_file()
    assert (work / ".rlvr" / "build.py").is_file()


@pytest.fixture
def pack_copy(tmp_path):
    return shutil.copytree(load_pack(YAMLCPP).root, tmp_path / "pack")


def test_edited_identity_no_longer_matches_task_id(pack_copy):
    identity = json.loads((pack_copy / "identity.json").read_text())
    identity["instruction"] += " (edited)"
    (pack_copy / "identity.json").write_text(json.dumps(identity))
    with pytest.raises(PackError, match="task_id"):
        load_pack(pack_copy)


def test_ref_digest_must_match_identity(pack_copy):
    refs = json.loads((pack_copy / "artifact_refs.json").read_text())
    refs["verifier"]["sha256"] = "0" * 64
    (pack_copy / "artifact_refs.json").write_text(json.dumps(refs))
    with pytest.raises(PackError, match="verifier digest"):
        load_pack(pack_copy)


def test_corrupted_archive_is_refused(pack_copy, tmp_path):
    archive = pack_copy / "workspace.tar.zst"
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 0xFF
    archive.write_bytes(bytes(data))
    pack = load_pack(pack_copy)
    (tmp_path / "s").mkdir()
    with pytest.raises(Exception):  # noqa: B017 - rlvr raises its own archive/digest errors
        pack.extract("workspace", tmp_path / "w", tmp_path / "s")
    assert not (tmp_path / "w" / "src").exists()


def test_missing_archive_and_unknown_names(pack_copy):
    (pack_copy / "verifier.tar.zst").unlink()
    with pytest.raises(PackError, match="verifier.tar.zst"):
        load_pack(pack_copy)
    with pytest.raises(PackError, match="no task pack"):
        load_pack("no-such-pack")
