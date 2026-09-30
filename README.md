# honeminer

An automatic Claude CLI miner harness for Hone (Bittensor SN5) V4 tasks.

When a task arrives, honeminer:
1. scans the workspace with plain code (no AI): language, build and test commands, the docs that matter, read-only paths;
2. writes a task-specific `CLAUDE.md`, prompt, hooks-only settings and hook scripts;
3. runs Claude CLI unattended (`claude -p`, bypass mode, no permission prompts) in a no-network sandbox built
   from the grading image;
4. whenever Claude tries to stop, re-checks the work on clean copies with the validator's own code (apply, build,
   each of Claude's checks must fail on the original and pass on the fix, existing tests still pass) and sends
   the exact failure back until it passes, time runs out, or the run stalls;
5. keeps the best answer so far and grades it locally with rlvr's grader; everything lands in `runs/`;
6. builds the V4 work log (`trajectory.json`, trajectory_v1) from the model traffic the gateway recorded, with plain
   code and no extra model calls. It is built after Claude stops, so it never changes the run; it is always
   valid under rlvr's `parse_trajectory` and never larger than the size limit (it shrinks by condensing responses,
   trimming long outputs, then dropping the oldest turns, each step labeled).

Live mining (testnet/mainnet) is not enabled yet: the offer server (`serve`) is the next step.

## Requirements

- Linux, Docker usable by a **non-root** user (rlvr refuses to grade as root)
- Python 3.10-3.12
- Claude Code installed natively (`claude --version`), and either `claude setup-token` (Max plan) or an API key
- The grading image: `docker pull public.ecr.aws/t3h1r6x1/hone-subnet/polyglot-sandbox@sha256:87f7ea82...`
  (if the pull is rate-limited, build `hone-subnet/docker/polyglot-sandbox`, push it to a local registry
  and set `HONEMINER_IMAGE=localhost:5000/...@sha256:...`)

## Install

```bash
git clone https://github.com/hone-subnet-org/hone-subnet ../hone-subnet
git -C ../hone-subnet checkout d5786a9bfd3a52cf8e89795d196a7d40cc3d2751
python -m venv .venv && . .venv/bin/activate
pip install -e '../hone-subnet[miner]' -e '.[dev]'
cp .env.example .env            # then set CLAUDE_CODE_OAUTH_TOKEN (or HONEMINER_AUTH=api_key + ANTHROPIC_API_KEY)
python -m honeminer config      # effective settings, secrets masked
```

## Run locally

```bash
python -m honeminer doctor            # host checks
python -m honeminer doctor --spike    # Phase 0 go/no-go: one real sandboxed Claude run through the gateway
python -m honeminer pack              # build + prove every recipe pack (C, C++, Rust, Go, Python, Node, Java, Bash)
python -m honeminer kit cpp-yamlcpp   # see the CLAUDE.md and prompt Claude will get
python -m honeminer solve cpp-yamlcpp --runs 3
python -m honeminer bench --runs 3    # every pack in packs/; prints pass/fail per run
python -m honeminer grade cpp-yamlcpp my.diff   # grade any diff as a validator would
```

Each solve writes `runs/<time>-<task>/` (facts, CLAUDE.md, prompt, Claude's stream, gate rounds, checks, the
shipped diff or script, the grade, the recorded `traffic.jsonl` and the `trajectory.json` work log) and appends one
line to `runs/index.jsonl`. `runs/` holds tasks and model traffic: keep it private.

## Settings

Every setting is in [`.env.example`](.env.example): model and effort, the 20-minute task budget and reserves,
the audit round, time notices, sandbox size, auth, concurrency, archive size. A real environment variable wins
over `.env`, and an invalid value stops startup.

## Develop

```bash
pytest -q -m "not docker and not live"   # fast
pytest -q -m docker                      # sandbox image + Docker, as a non-root user (CI runs these)
ruff check honeminer tests
```
