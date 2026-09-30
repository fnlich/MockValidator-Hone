from honeminer.answer import AnswerKeeper, Rank


def test_empty_or_weaker_never_replaces_a_checked_answer():
    keeper = AnswerKeeper()
    assert keeper.offer(b"good", Rank.CHECKED)
    assert not keeper.offer(b"", Rank.CHECKED)
    assert not keeper.offer(b"fragment", Rank.STATIC_OK)
    assert not keeper.offer(b"almost", Rank.BUILDS)
    assert keeper.best.content == b"good" and keeper.best.rank is Rank.CHECKED


def test_latest_wins_among_equals_and_better_replaces():
    keeper = AnswerKeeper()
    keeper.offer(b"v1", Rank.APPLIES)
    keeper.offer(b"v2", Rank.APPLIES)
    assert keeper.best.content == b"v2"
    keeper.offer(b"v3", Rank.CHECKED)
    assert keeper.best.content == b"v3"


def test_empty_is_held_only_when_nothing_else_exists():
    keeper = AnswerKeeper()
    assert keeper.best is None
    keeper.offer(b"", Rank.CHECKED)
    assert keeper.best.rank is Rank.EMPTY
    keeper.offer(b"x", Rank.STATIC_OK)
    assert keeper.best.content == b"x"


def test_verdict_memo_by_content():
    keeper = AnswerKeeper()
    keeper.offer(b"patch", Rank.CHECKED, "gate passed")
    keeper.offer(b"patch", Rank.STATIC_OK)
    assert keeper.verdict(b"patch") == (Rank.CHECKED, "gate passed")
    assert keeper.verdict(b"other") is None
