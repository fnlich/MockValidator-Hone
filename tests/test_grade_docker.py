"""Grading the real yaml-cpp task in the sandbox image (CI docker job)."""

import pytest

from honeminer.config import load_env
from honeminer.grade import grade_submission
from honeminer.tasks import load_pack

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def pack():
    return load_pack("cpp-yamlcpp")


@pytest.fixture(scope="module")
def image():
    return load_env().image


def test_reference_passes_all_six_checks(pack, image):
    result = grade_submission(pack, pack.reference(), image=image)
    assert result.passed, result.render()
    assert len(result.checks) == 6 and all(outcome == "passed" for _, outcome in result.checks)


def test_empty_patch_fails(pack, image):
    result = grade_submission(pack, b"", image=image)
    assert not result.passed and result.status != "abandoned", result.render()


def test_half_fix_fails(pack, image):
    reference = pack.reference().decode()
    half = reference[: reference.index("--- a/src/eventarchive_store.cpp")].encode()
    result = grade_submission(pack, half, image=image)
    assert result.status == "failed", result.render()
