"""A whole rehearsal round with real grading in the sandbox image (CI docker job, non-root)."""

import pytest

from honeminer import rehearsal as rh
from honeminer.config import load_env
from honeminer.pack import RECIPES_DIR, build_and_validate, load_recipe
from honeminer.tasks import load_pack

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def stats_pack(tmp_path_factory):
    out = tmp_path_factory.mktemp("packs") / "python-stats"
    assert build_and_validate(load_recipe(RECIPES_DIR / "python-stats"), out, image=load_env().image).ok
    return load_pack(out)


@pytest.mark.parametrize("over_http", [False, True], ids=["in-process", "http"])
def test_reference_agent_round_is_graded_and_paid(stats_pack, tmp_path, over_http):
    settings = load_env(None, environ={"HONEMINER_RUNS_DIR": str(tmp_path / "runs"),
                                       "HONEMINER_IMAGE": load_env().image})
    rehearsal, root = rh.run_rehearsal(stats_pack, settings, agent="reference", over_http=over_http)
    assert rehearsal.result.status == "completed", rehearsal.result.reason
    grades = {row["miner"]: (row["commit"], row["grade"]) for row in rehearsal.rows}
    assert grades[rh.HONEMINER] == ("grant", "passed") and grades[rh.EMPTY][1] in ("failed", "rejected")
    assert rehearsal.exit_code == 0 and (root / "rehearsal.json").is_file() and (root / "offer.json").is_file()
    assert row_signed(rehearsal) == over_http


def row_signed(rehearsal):
    return next(row for row in rehearsal.rows if row["miner"] == rh.HONEMINER)["signed"]
