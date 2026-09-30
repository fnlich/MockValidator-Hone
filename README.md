# honeminer

An automatic miner harness for Hone (Bittensor SN5) V4 tasks. When a task arrives it
scans the workspace, generates a task-specific `CLAUDE.md`, prompt and hooks, runs
Claude CLI unattended in a no-network sandbox, re-checks the result on a clean copy
with the validator's own code, and ships the best checked answer.

Status: under construction, built step by step (see the commit history). This first
step provides the settings loader and the development setup.

## Install

```bash
git clone https://github.com/hone-subnet-org/hone-subnet ../hone-subnet
git -C ../hone-subnet checkout d5786a9bfd3a52cf8e89795d196a7d40cc3d2751
python -m venv .venv && . .venv/bin/activate
pip install -e '../hone-subnet[miner]' -e '.[dev]'
cp .env.example .env        # then edit
python -m honeminer config  # effective settings, secrets masked
```

## Settings

Every setting is in [`.env.example`](.env.example). A real environment variable always
wins over `.env`, and an invalid value stops startup with an error. Testnet and mainnet
modes refuse to start until `NETUID`, `HONEMINER_ANTHROPIC_AUTHORIZATION` and the
credential for `HONEMINER_AUTH` are set, and until the work log (trajectory) is built
and enabled.

## Tests

```bash
pytest -q -m "not docker and not live"   # fast
pytest -q -m docker                      # needs Docker and the sandbox image; run as a non-root user
```
