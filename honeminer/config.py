"""One settings loader for every entry point.

Values come from ``.env`` and the process environment; a real environment
variable always wins over ``.env``. Every setting is validated here, so a typo
fails loudly at startup instead of silently changing behaviour mid-solve.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path

MODES = ("local", "testnet", "mainnet")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
AUTHS = ("oauth", "api_key")

_DOTENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_SIZE = re.compile(r"^([0-9]+)([kmg]?)$")


class ConfigError(ValueError):
    """A setting is missing or invalid."""


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines; ``#`` starts a comment outside quotes."""

    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _DOTENV_LINE.match(line)
        if match is None:
            raise ConfigError(f".env line {number} is not KEY=VALUE")
        key, raw = match.groups()
        raw = raw.strip()
        if raw[:1] in ("'", '"'):
            quote = raw[0]
            end = raw.find(quote, 1)
            if end < 0:
                raise ConfigError(f".env line {number} has an unterminated quote")
            value = raw[1:end]
        else:
            value = raw.split("#", 1)[0].strip()
        values[key] = value
    return values


def _text(value: str) -> str:
    return value


def _choice(options: tuple[str, ...]) -> Callable[[str], str]:
    def parse(value: str) -> str:
        if value not in options:
            raise ValueError(f"must be one of {', '.join(options)}")
        return value

    return parse


def _int(low: int, high: int | None = None) -> Callable[[str], int]:
    def parse(value: str) -> int:
        number = int(value)
        if number < low or (high is not None and number > high):
            raise ValueError(f"must be between {low} and {high}" if high else f"must be >= {low}")
        return number

    return parse


def _optional_int(low: int) -> Callable[[str], int | None]:
    inner = _int(low)
    return lambda value: None if value == "" else inner(value)


def _float(low: float, high: float) -> Callable[[str], float]:
    def parse(value: str) -> float:
        number = float(value)
        if not low <= number <= high:
            raise ValueError(f"must be between {low} and {high}")
        return number

    return parse


def _optional_float(low: float) -> Callable[[str], float | None]:
    def parse(value: str) -> float | None:
        if value == "":
            return None
        number = float(value)
        if number <= low:
            raise ValueError(f"must be > {low}")
        return number

    return parse


def _bool(value: str) -> bool:
    lowered = value.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ValueError("must be true or false")


def _fractions(value: str) -> tuple[float, ...]:
    parts = tuple(float(item) for item in value.split(",") if item.strip())
    if any(not 0.0 < part < 1.0 for part in parts):
        raise ValueError("each fraction must be between 0 and 1")
    return tuple(sorted(parts, reverse=True))


def _size(value: str) -> int:
    match = _SIZE.match(value.lower())
    if match is None:
        raise ValueError("must look like 512m or 8g")
    number, unit = match.groups()
    return int(number) * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[unit]


def _image(value: str) -> str:
    if value and "@sha256:" not in value:
        raise ValueError("must be digest-pinned (name@sha256:...)")
    return value


@dataclass(frozen=True)
class Setting:
    env: str
    default: str
    parse: Callable[[str], object]
    secret: bool = False


SETTINGS: dict[str, Setting] = {
    # Mode and network
    "mode": Setting("HONEMINER_MODE", "local", _choice(MODES)),
    "subtensor_network": Setting("SUBTENSOR_NETWORK", "test", _text),
    "netuid": Setting("NETUID", "", _optional_int(0)),
    "wallet_name": Setting("WALLET_NAME", "default", _text),
    "wallet_hotkey": Setting("WALLET_HOTKEY", "default", _text),
    "axon_host": Setting("AXON_HOST", "0.0.0.0", _text),
    "axon_port": Setting("AXON_PORT", "8091", _int(1, 65_535)),
    "axon_external_ip": Setting("AXON_EXTERNAL_IP", "", _text),
    "anthropic_authorization": Setting("HONEMINER_ANTHROPIC_AUTHORIZATION", "", _text),
    # Claude CLI and auth
    "claude_bin": Setting("HONEMINER_CLAUDE_BIN", "", _text),
    "model": Setting("HONEMINER_MODEL", "claude-opus-5-5", _text),
    "effort": Setting("HONEMINER_EFFORT", "high", _choice(EFFORTS)),
    "auth": Setting("HONEMINER_AUTH", "oauth", _choice(AUTHS)),
    "oauth_token": Setting("CLAUDE_CODE_OAUTH_TOKEN", "", _text, secret=True),
    "api_key": Setting("ANTHROPIC_API_KEY", "", _text, secret=True),
    "max_budget_usd": Setting("HONEMINER_MAX_BUDGET_USD", "", _optional_float(0.0)),
    # Serving live offers (`serve`)
    "min_solve_s": Setting("HONEMINER_MIN_SOLVE_S", "300", _int(0, 3600)),
    "max_request_bytes": Setting("HONEMINER_MAX_REQUEST_BYTES", "1000000", _int(1024, 10_000_000)),
    "metagraph_sync_s": Setting("HONEMINER_METAGRAPH_SYNC_S", "300", _int(10, 86_400)),
    "min_validator_stake": Setting("HONEMINER_MIN_VALIDATOR_STAKE", "0", _float(0.0, 1e12)),
    "require_validator_permit": Setting("HONEMINER_REQUIRE_VALIDATOR_PERMIT", "on", _bool),
    # Concurrency
    "slots": Setting("HONEMINER_SLOTS", "1", _int(1, 64)),
    # Clock
    "task_budget_s": Setting("HONEMINER_TASK_BUDGET_S", "1200", _int(60, 3600)),
    "expiry_margin_s": Setting("HONEMINER_EXPIRY_MARGIN_S", "45", _int(30, 600)),
    "reserve_final_check_s": Setting("HONEMINER_RESERVE_FINAL_CHECK_S", "30", _int(0, 600)),
    "reserve_upload_s": Setting("HONEMINER_RESERVE_UPLOAD_S", "20", _int(0, 600)),
    "reserve_cleanup_s": Setting("HONEMINER_RESERVE_CLEANUP_S", "5", _int(0, 600)),
    "gate_round_s": Setting("HONEMINER_GATE_ROUND_S", "60", _int(1, 1800)),
    "verify_facts_s": Setting("HONEMINER_VERIFY_FACTS_S", "180", _int(5, 1800)),
    "audit_round_min_left": Setting("HONEMINER_AUDIT_ROUND_MIN_LEFT", "0.40", _float(0.0, 1.0)),
    "time_notices": Setting("HONEMINER_TIME_NOTICES", "0.50,0.25,0.10", _fractions),
    # Trajectory (work log): built after Claude stops, from recorded traffic; never touches the solve
    "trajectory": Setting("HONEMINER_TRAJECTORY", "on", _bool),
    "trajectory_max_bytes": Setting("HONEMINER_TRAJECTORY_MAX_BYTES", "", _optional_int(1)),
    "trajectory_build_s": Setting("HONEMINER_TRAJECTORY_BUILD_S", "5", _float(0.5, 60.0)),
    # Rehearsal (a local validator + problem server round; see `rehearse`)
    "rehearsal_lease_s": Setting("HONEMINER_REHEARSAL_LEASE_S", "1500", _int(600, 7200)),
    "rehearsal_trajectory_max_bytes": Setting("HONEMINER_REHEARSAL_TRAJECTORY_MAX_BYTES", "67108864",
                                              _int(1, 64 * 1024**2)),
    # Sandbox
    "image": Setting("HONEMINER_IMAGE", "", _image),
    "agent_cpus": Setting("HONEMINER_AGENT_CPUS", "4", _int(1, 256)),
    "agent_memory": Setting("HONEMINER_AGENT_MEMORY", "8g", _size),
    "agent_pids": Setting("HONEMINER_AGENT_PIDS", "4096", _int(64, 1_000_000)),
    "agent_scratch": Setting("HONEMINER_AGENT_SCRATCH", "8g", _size),
    "stop_hook_block_cap": Setting("HONEMINER_STOP_HOOK_BLOCK_CAP", "1000", _int(1)),
    # Archive
    "runs_dir": Setting("HONEMINER_RUNS_DIR", "runs", _text),
    "archive_workspaces": Setting("HONEMINER_ARCHIVE_WORKSPACES", "true", _bool),
    "archive_max_gb": Setting("HONEMINER_ARCHIVE_MAX_GB", "50", _float(0.0, 1e6)),
}


@dataclass(frozen=True)
class Settings:
    mode: str
    subtensor_network: str
    netuid: int | None
    wallet_name: str
    wallet_hotkey: str
    axon_host: str
    axon_port: int
    axon_external_ip: str
    anthropic_authorization: str
    claude_bin: str
    model: str
    effort: str
    auth: str
    oauth_token: str
    api_key: str
    max_budget_usd: float | None
    min_solve_s: int
    max_request_bytes: int
    metagraph_sync_s: int
    min_validator_stake: float
    require_validator_permit: bool
    slots: int
    task_budget_s: int
    expiry_margin_s: int
    reserve_final_check_s: int
    reserve_upload_s: int
    reserve_cleanup_s: int
    gate_round_s: int
    verify_facts_s: int
    audit_round_min_left: float
    time_notices: tuple[float, ...]
    trajectory: bool
    trajectory_max_bytes: int | None
    trajectory_build_s: float
    rehearsal_lease_s: int
    rehearsal_trajectory_max_bytes: int
    image: str
    agent_cpus: int
    agent_memory: int
    agent_pids: int
    agent_scratch: int
    stop_hook_block_cap: int
    runs_dir: str
    archive_workspaces: bool
    archive_max_gb: float

    @property
    def live(self) -> bool:
        return self.mode != "local"

    def live_blockers(self) -> list[str]:
        """Reasons this configuration may not answer real validator offers."""

        problems: list[str] = []
        if self.netuid is None:
            problems.append("NETUID is empty")
        if not self.anthropic_authorization.strip():
            problems.append("HONEMINER_ANTHROPIC_AUTHORIZATION is empty")
        if not self.trajectory:
            problems.append("HONEMINER_TRAJECTORY is off; V4 replies need a trajectory")
        if self.auth == "oauth" and not self.oauth_token:
            problems.append("HONEMINER_AUTH=oauth but CLAUDE_CODE_OAUTH_TOKEN is empty")
        if self.auth == "api_key" and not self.api_key:
            problems.append("HONEMINER_AUTH=api_key but ANTHROPIC_API_KEY is empty")
        return problems

    def require_live(self) -> None:
        problems = self.live_blockers()
        if problems:
            raise ConfigError("live mode refused: " + "; ".join(problems))

    def redacted(self) -> dict[str, object]:
        """Effective values for display, with secrets masked."""

        shown: dict[str, object] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if SETTINGS[item.name].secret and value:
                value = "***set***"
            shown[SETTINGS[item.name].env] = value
        return shown


def load_env(
    path: str | os.PathLike[str] | None = ".env",
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load settings from ``path`` (if it exists) overlaid by ``environ``."""

    environ = os.environ if environ is None else environ
    file_values: dict[str, str] = {}
    if path is not None:
        env_file = Path(path)
        if env_file.is_file():
            file_values = parse_dotenv(env_file.read_text(encoding="utf-8"))

    parsed: dict[str, object] = {}
    errors: list[str] = []
    for name, setting in SETTINGS.items():
        raw = environ.get(setting.env, file_values.get(setting.env, setting.default))
        try:
            parsed[name] = setting.parse(raw.strip())
        except ValueError as exc:
            errors.append(f"{setting.env}={raw!r}: {exc}")
    if errors:
        raise ConfigError("invalid settings: " + "; ".join(errors))

    return Settings(**parsed)  # type: ignore[arg-type]
