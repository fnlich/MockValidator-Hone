"""Build every recipe in the grading image and prove each pack (CI docker job)."""

import pytest

from honeminer.config import load_env
from honeminer.pack import RECIPES_DIR, build_and_validate, load_recipe

pytestmark = pytest.mark.docker

RECIPES = sorted(p.name for p in RECIPES_DIR.iterdir() if (p / "recipe.json").is_file())


@pytest.mark.parametrize("name", RECIPES)
def test_recipe_builds_a_valid_pack(name, tmp_path):
    result = build_and_validate(load_recipe(RECIPES_DIR / name), tmp_path / name, image=load_env().image)
    assert result.reference == "passed"
    assert result.empty in ("failed", "rejected")
    assert result.deterministic
