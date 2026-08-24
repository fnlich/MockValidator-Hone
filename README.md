# hone-miner-harness

A local mock validator for [Hone Subnet](https://github.com/hone-subnet-org/hone-subnet) (Bittensor SN5) miners.

It reproduces exactly what a real validator does to your miner — sign a problem, dispatch it, authenticate the reply, grade it against hidden tests — with **no chain, no registration, and no problem server**. That closes the gap that otherwise only opens on mainnet, where a mistake costs real TAO.

```
derive a per-miner request id
  -> sign a TaskRequest for that miner's hotkey
  -> POST /solve
  -> verify the reply is signed BY the miner and FOR us
  -> grade the returned code against HIDDEN tests in the real sandbox
  -> report the payment the subnet would have assigned
```

Every security-relevant step calls the validator's own code from the `rlvr` package (`sign_message`, `verify_signature`, `derive_request_id`, `Verifier`, `compute_payments`). Nothing is re-implemented: a harness that rolled its own signing could drift from the real protocol and tell you a broken miner is fine, which is the one thing a harness must never do.

## Install

Requires the subnet package, because the harness grades with the validator's real sandbox:

```bash
git clone https://github.com/hone-subnet-org/hone-subnet
pip install -e './hone-subnet[chain,miner]'
pip install -e .
```

## Use

**Against the built-in demo miner** (nothing else needed):

```bash
python run_local_miner.py          # terminal 1
python mock_validator.py           # terminal 2
```

```
[FAIL] run-length-encode            4/5 hidden  pay=0.000  0.0s
[SKIP] rust-sum-stdin               0/0 hidden  pay=0.000  0.0s
         skipped: cannot grade rust here: Rust challenge execution requires the Docker executor
[PASS] sum-of-digits                5/5 hidden  pay=1.000  0.0s

1/2 solved   total payment 1.000   (1 skipped)
(the subnet pays only for a FULL hidden-suite pass; partial == zero)
```

That output is the whole point. `run-length-encode` passes **4 of 5** hidden tests and earns **nothing** — the built-in solver is wrong on purpose so a first run shows you the subnet's real economics rather than a wall of green.

**Against your own solver:**

```bash
python run_local_miner.py --solver mypkg.mymodule:MySolver
python mock_validator.py -v
```

`--solver` takes `module:attribute` naming a Solver instance or a zero-arg callable returning one. A Solver is anything with:

```python
async def solve_task(task, timeout_s) -> object with .code and .raw_response
async def aclose() -> None
```

which is the same seam `examples/custom_miner` in the hone-subnet repo uses, so a solver written against that runs here unchanged.

**Against a miner you already run:**

```bash
python mock_validator.py --url http://your-host:8091 --miner-hotkey 5F...
```

Your miner will reject the harness's key with `403 unauthorized signer`, because it holds no validator permit. Either run it behind `run_local_miner.py`, or start it with `MINER_REQUIRE_VALIDATOR_PERMIT=false` **while testing only**.

## Dispatching to a pool

A real validator deals each problem to many miners at once, and that changes the
arithmetic: the payment formula's latency term is relative to the **fastest
correct responder**, so against a single miner it is always `1.0` and the
0.95-1.0 spread never appears. Pool pass-rate and the difficulty band are
pool-level signals too.

```bash
python mock_validator.py --miner 127.0.0.1:8101=//M1 \
                         --miner 127.0.0.1:8102=//M2 \
                         --miner 10.0.0.7:8091=5F...
```

`HOST:PORT[=HOTKEY]`, repeatable. `HOTKEY` is an ss58 address, or a `//Dev` URI
for local testing; omit it and the harness dev key is used. It is not cosmetic —
the hotkey is folded into the per-miner request id and must match the reply's
`Epistula-Signed-By`, so a wrong one makes an honest miner look unauthenticated.

Or from a file, `--miners pool.json`:

```json
[{"uid": 1, "host": "10.0.0.7", "port": 8091, "hotkey": "5F..."},
 {"uid": 2, "host": "10.0.0.8", "port": 8091, "hotkey": "5G..."}]
```

A real round against three miners — one fast and correct, one correct but 3s
slower, one subtly wrong:

```
=== run-length-encode ===
  [PASS] uid1 127.0.0.1:8101      5GzrAe…mNME    5/5 hidden  pay=1.0000    0.02s
  [PASS] uid2 127.0.0.1:8102      5Fhgqg…5xUm    5/5 hidden  pay=0.9994    3.02s
  [FAIL] uid3 127.0.0.1:8103      5FC2Qt…FTpv    4/5 hidden  pay=0.0000    0.02s
  pool pass-rate 67%  band=easy

=== leaderboard over 2 graded problem(s), 1 skipped ===
  1. uid1 127.0.0.1:8101      solved 2/2   payment 2.0000   weight share  40.0%
  2. uid2 127.0.0.1:8102      solved 2/2   payment 1.9989   weight share  40.0%
  3. uid3 127.0.0.1:8103      solved 1/2   payment 1.0000   weight share  20.0%
```

Three things to read out of that. `pay=0.9994` is the latency tiebreaker being
applied for real — `0.95 + 0.05·2^(-3000/180000)`. `4/5 hidden` pays **zero**.
And uid1 and uid2 differ by 0.001 in payment yet land on the same 40.0% weight
share, which is the flat-scoring compression the subnet is built on.

Fan-out is concurrent, with dispatch and grading throttled separately
(`--dispatch-concurrency`, `--verify-concurrency`) — the same split the real
validator makes between its I/O-bound fan-out and its sandbox-bound grading.

To run several local miners for testing, give each its own port and hotkey:

```bash
python run_local_miner.py --port 8101 --miner-uri //M1 --solver mypkg:FastSolver
python run_local_miner.py --port 8102 --miner-uri //M2 --solver mypkg:OtherSolver
```

### Options

| Flag | Default | Meaning |
|---|---|---|
| `--miner` | — | `HOST:PORT[=HOTKEY]`, repeatable, one per miner |
| `--miners` | — | JSON file describing the pool |
| `--url` | `http://127.0.0.1:8091` | Single-miner shorthand |
| `--dispatch-concurrency` | `64` | Miners contacted at once |
| `--verify-concurrency` | `4` | Sandboxes grading at once |
| `--problem` | (all) | Only problems whose name contains this substring |
| `--miner-hotkey` | harness dev key | The miner's ss58 hotkey |
| `--executor` | `subprocess` | Grading sandbox; `docker` also enables Rust |
| `--deadline` | `120` | Seconds advertised to the miner |
| `-v` | off | Print the code the miner returned |

Exit status is `0` when at least one miner fully solved every graded problem, `1` otherwise, so it drops straight into CI.

## Problems

`problems/*.json`. Each carries **public examples** (sent to the miner) and **hidden tests** (kept back, used for grading) — the split that defines the subnet:

```json
{
  "name": "sum-of-digits",
  "language": "python",
  "entrypoint": "sum_of_digits",
  "statement": "Return the sum of the decimal digits of a non-negative integer n.",
  "public_examples": [{"args": [12345], "kwargs": {}, "expected": 15}],
  "hidden_tests":   [{"args": [0], "kwargs": {}, "expected": 0}]
}
```

Add your own by dropping a file in. For `"language": "rust"`, `args` is a single stdin string and `expected` is the stdout to match token-by-token; grading needs `--executor docker` and the pinned sandbox image.

## What it checks

The four acceptance checks a real validator applies before it will grade anything, each verified against a real signature:

1. the reply is HTTP 200 and within the 128 KB cap;
2. `Epistula-Signed-By` is the miner's hotkey;
3. the signature verifies **and** is bound to the calling validator;
4. `problem_id` echoes the per-dispatch request id.

Confirmed failing when they should:

```
unauthorized validator key  -> HTTP 403 {"error":"unauthorized signer"}
wrong miner hotkey          -> HTTP 401 {"error":"invalid signature"}
```

## Limits

- **A pass here is not a guarantee of a pass on the subnet.** Real problems come from a closed-source server and are far harder than these samples; the hidden tests here are ones you wrote.
- Grading defaults to the `subprocess` executor, which the subnet documents as dev-grade. That is appropriate here — the code being run is your own solver's output, not an adversary's — but use `--executor docker` to grade the way a validator actually will.
- The harness signs with well-known dev keys and `run_local_miner.py` trusts a fabricated metagraph. It is a test rig: never expose it to the internet or point it at a real wallet.
