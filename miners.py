"""Describing the miner pool a mock round dispatches to."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Miner:
    """One dispatch target: where it listens and which hotkey signs its replies.

    ``hotkey`` is not cosmetic. It is folded into the per-miner request id and
    it is the identity the reply's ``Epistula-Signed-By`` must match, so a wrong
    hotkey here makes an honest miner look unauthenticated.
    """

    uid: int
    host: str
    port: int
    hotkey: str

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def label(self) -> str:
        return f"uid{self.uid} {self.host}:{self.port}"

    @property
    def short_hotkey(self) -> str:
        return f"{self.hotkey[:6]}…{self.hotkey[-4:]}" if len(self.hotkey) > 12 else self.hotkey


def _resolve_hotkey(value: str) -> str:
    """Accept a real ss58 address or a ``//Dev`` URI for local testing."""
    if value.startswith("//"):
        from mock_validator import keypair

        return keypair(value).ss58_address
    return value


def parse_miner(spec: str, uid: int, default_hotkey: str) -> Miner:
    """Parse ``host:port`` or ``host:port=hotkey`` (hotkey may be a //Dev URI).

    Splitting the hotkey off with ``=`` rather than another colon keeps IPv6
    hosts and ``//Dev`` URIs from turning the spec into a guessing game.
    """
    address, _, hotkey = spec.partition("=")
    address = address.strip()
    if ":" not in address:
        raise ValueError(f"{spec!r}: expected host:port[=hotkey]")
    host, _, port_text = address.rpartition(":")
    host = host.strip("[]") or "127.0.0.1"
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(f"{spec!r}: {port_text!r} is not a port") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"{spec!r}: port out of range")
    resolved = _resolve_hotkey(hotkey.strip()) if hotkey.strip() else default_hotkey
    return Miner(uid=uid, host=host, port=port, hotkey=resolved)


def load_miner_file(path: Path, default_hotkey: str) -> list[Miner]:
    """Load a JSON list of ``{uid?, host, port, hotkey?}`` objects."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of miners")
    miners: list[Miner] = []
    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}[{index}]: expected an object")
        if "host" not in entry or "port" not in entry:
            raise ValueError(f"{path}[{index}]: needs 'host' and 'port'")
        hotkey = entry.get("hotkey")
        miners.append(
            Miner(
                uid=int(entry.get("uid", index + 1)),
                host=str(entry["host"]),
                port=int(entry["port"]),
                hotkey=_resolve_hotkey(str(hotkey)) if hotkey else default_hotkey,
            )
        )
    return miners


def build_pool(
    specs: list[str],
    miner_file: Optional[str],
    url: Optional[str],
    default_hotkey: str,
) -> list[Miner]:
    """Assemble the pool from --miner / --miners / legacy --url, in that order."""
    miners: list[Miner] = []
    if miner_file:
        miners.extend(load_miner_file(Path(miner_file), default_hotkey))
    for spec in specs or []:
        miners.append(parse_miner(spec, uid=len(miners) + 1, default_hotkey=default_hotkey))
    if not miners and url:
        cleaned = url.split("//", 1)[-1].rstrip("/")
        miners.append(parse_miner(cleaned, uid=1, default_hotkey=default_hotkey))
    # Duplicate UIDs would collide in the payment map, which is keyed by uid.
    seen: set[int] = set()
    for miner in miners:
        if miner.uid in seen:
            raise ValueError(f"duplicate uid {miner.uid} in the miner pool")
        seen.add(miner.uid)
    return miners
