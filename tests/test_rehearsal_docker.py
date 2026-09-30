"""A whole rehearsal round with real grading in the sandbox image (CI docker job, non-root)."""

import pytest

from honeminer import rehearsal as rh
from honeminer.config import load_env
from honeminer.pack import RECIPES_DIR, build_and_validate, load_recipe
from honeminer.tasks import load_pack

pytestmark = pytest.mark.docker


def test_reference_agent_round_is_graded_and_paid(tmp_path):
    settings = load_env(None, environ={"HONEMINER_RUNS_DIR": str(tmp_path / "runs"),
                                       "HONEMINER_IMAGE": load_env().image})
    built = build_and_validate(load_recipe(RECIPES_DIR / "python-stats"), tmp_path / "python-stats",
                               image=settings.image)
    assert built.ok
    rehearsal, root = rh.run_rehearsal(load_pack(tmp_path / "python-stats"), settings, agent="reference")
    assert rehearsal.result.status == "completed", rehearsal.result.reason
    grades = {row["miner"]: (row["commit"], row["grade"]) for row in rehearsal.rows}
    assert grades[rh.HONEMINER] == ("grant", "passed") and grades[rh.EMPTY][1] in ("failed", "rejected")
    assert rehearsal.exit_code == 0 and (root / "rehearsal.json").is_file() and (root / "offer.json").is_file()
