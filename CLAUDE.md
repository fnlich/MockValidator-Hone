# honeminer: automatic Claude CLI miner harness for Hone V4

## Commands
- Setup: `pip install -e '../hone-subnet[miner]' -e '.[dev]'` (hone-subnet checked out at d5786a9)
- Fast tests (no Docker, no model): `pytest -q -m "not docker and not live"`
- One test: `pytest -q tests/test_config.py::test_defaults_load_without_env_file`
- Docker tests (Docker + sandbox image, run as a non-root user): `pytest -q -m docker`
- Lint: `ruff check honeminer tests`
- Effective settings: `python -m honeminer config`

## Workflow
- Done = ruff clean and fast tests pass; paste the command and its output in the summary.
- For a bug, write a failing test that reproduces it first, then fix.
- Never modify or skip an existing test to make it pass. If a test looks wrong, stop and explain.
- In the solve path, prefer deterministic checks (rlvr, git, compilers) over model judgment.

## Architecture decisions
- rlvr (hone-subnet @ d5786a9) is the oracle: grading, static_rejection, validate_script come from it.
- Every setting lives in honeminer/config.py and .env.example; nothing else reads os.environ directly.
- One clock (honeminer/clock.py); no other module computes a deadline or uses a fixed timeout cap.
- Claude CLI is the only solver backend. Credentials stay in the host gateway, never in the sandbox.

## Gotchas
- rlvr refuses to grade as root (`round_policy`), so Docker tests need a non-root user.
- Task builds may rewrite tracked files (.prebuilt/, .rlvr/); the patch builder resets them.
- The grader mounts the workspace at /work and runs terminal scripts as /submission/script.sh.

## Git
- Branch claude/beautiful-gates-37ocl0; no PR unless asked; one commit per plan step.
