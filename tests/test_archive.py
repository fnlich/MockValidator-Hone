import os

from honeminer.archive import prune_runs


def make_run(runs, name, size, age):
    run = runs / name
    run.mkdir(parents=True)
    (run / "blob").write_bytes(b"x" * size)
    os.utime(run, (age, age))
    return run


def test_oldest_runs_are_pruned_down_to_the_cap_and_summaries_are_kept(tmp_path):
    runs = tmp_path / "runs"
    old = make_run(runs, "20260101T000000Z-a", 4000, 1_000)
    middle = make_run(runs, "20260102T000000Z-b", 4000, 2_000)
    current = make_run(runs, "20260103T000000Z-c", 4000, 3_000)
    (runs / "index.jsonl").write_text("{}\n" * 1000)
    (runs / "offers.jsonl").write_text("{}\n")
    removed = prune_runs(runs, max_bytes=12_000, keep=current)  # 3 runs + 3 KB of summaries: one must go
    assert removed == [old] and not old.exists() and middle.exists() and current.exists()
    assert (runs / "index.jsonl").exists() and (runs / "offers.jsonl").exists()
    assert prune_runs(runs, max_bytes=1, keep=current) == [middle] and current.exists()  # never the current run


def test_no_cap_means_nothing_is_pruned(tmp_path):
    runs = tmp_path / "runs"
    run = make_run(runs, "20260101T000000Z-a", 4000, 1_000)
    assert prune_runs(runs, max_bytes=0) == [] and run.exists()
