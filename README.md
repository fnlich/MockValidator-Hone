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

7. for live offers, `serve` is the endpoint validators call: it checks each signed offer, solves it, uploads the
   answer and the work log to the offer's slots, and returns a signed reply (see "Mine on testnet").

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
python -m honeminer pack              # build + prove every recipe pack (C, Rust, Go, Python, Node, Java, Bash; C++ is the committed yaml-cpp pack)
python -m honeminer kit cpp-yamlcpp   # see the CLAUDE.md and prompt Claude will get
python -m honeminer solve cpp-yamlcpp --runs 3
python -m honeminer bench --runs 3    # every pack in packs/; prints pass/fail per run
python -m honeminer grade cpp-yamlcpp my.diff   # grade any diff as a validator would
```

### Rehearse a whole round

```bash
python -m honeminer rehearse python-stats --agent reference   # plumbing only, no model tokens
python -m honeminer rehearse cpp-yamlcpp                       # real Claude as honeminer
python -m honeminer rehearse cpp-yamlcpp --agent reference --trajectory-max-bytes 20000
```

A local problem server (on a fake https origin, no network) leases the pack and issues upload slots;
rlvr's own validator code (`evaluate_round`) sends the offer to six miners: honeminer, the reference answer, a
slower copy of it, an empty answer, a broken answer, and one that never answers. honeminer downloads the
workspace, solves, builds the work log for this challenge and hotkey, and uploads both artifacts. The server then
checks every reply strictly (refs match slots, uploads match the signed sha256/size, the work log parses and is
bound to this task, challenge, hotkey and answer), and the validator grades the grants in Docker and pays.
The table shows each miner's reply, commit verdict, grade, latency and payment; `runs/<time>-rehearsal-<pack>/`
keeps the offer and `rehearsal.json`. Exit code: 0 paid, 1 paid 0, 2 round abandoned. Add `--http` to reach
honeminer exactly as a validator does: through the real `serve` app, called by rlvr's `LiveSolverClient` with
Epistula-signed requests, and a signed reply the validator verifies before committing it.

Each solve writes `runs/<time>-<task>/` (facts, CLAUDE.md, prompt, Claude's stream, gate rounds, checks, the
shipped diff or script, the grade, the recorded `traffic.jsonl` and the `trajectory.json` work log) and appends one
line to `runs/index.jsonl`. `runs/` holds tasks and model traffic: keep it private.

## Mine on testnet

```bash
pip install -e '../hone-subnet[miner,chain]'    # adds bittensor (pinned by hone-subnet)
btcli subnet register --netuid <NETUID> --network test --wallet.name <W> --wallet.hotkey <H>
# .env: HONEMINER_MODE=testnet, NETUID, WALLET_NAME, WALLET_HOTKEY, AXON_PORT (open it), AXON_EXTERNAL_IP,
#       HONEMINER_ANTHROPIC_AUTHORIZATION, and the Claude credential
python -m honeminer doctor --spike                              # host, chain extras, one real Claude run
python -m honeminer rehearse cpp-yamlcpp --http                 # a whole signed round, locally
python -m honeminer serve                                       # advertise the axon and answer offers
```

`serve` refuses to start while any live setting is missing or a required host check fails. For each offer it:
- accepts it only if it is signed for our hotkey, fresh, not replayed, and from a registered validator with a
  permit (and at least `HONEMINER_MIN_VALIDATOR_STAKE`), for our slots and a supported task policy;
- queues it for one of `HONEMINER_SLOTS` solves, and refuses it at once (503) if Claude would get less than
  `HONEMINER_MIN_SOLVE_S`;
- solves it on the offer's own deadline, uploads the answer and the work log, and returns the signed reply;
  when no acceptable reply exists (for example, no valid log fits the slot), it uploads nothing and says why.

Private records (keep `runs/` private): `runs/offers.jsonl` (one line per offer: validator, status, error),
`runs/<time>-offer-<challenge>/` (the offer, the reply, the solve's archive), and `runs/notices.jsonl` (validators'
failure notices for tasks we answered). Stored tasks and answers are never used to answer offers.

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
