"""Durations in seconds, as text like "1h30m"."""


def format_duration(seconds):
    """Format whole seconds as e.g. "1h30m" or "45s"; 0 is "0s"."""
    if seconds < 0:
        raise ValueError("negative duration")
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return "".join(parts)


_UNITS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text):
    """Parse text like "1h30m" into whole seconds (see README)."""
    if not isinstance(text, str) or not text:
        raise ValueError(f"invalid duration: {text!r}")
    total = 0
    number = ""
    last_unit = -1
    order = "hms"
    for ch in text:
        if ch.isdigit() and ch.isascii():
            number += ch
            continue
        if ch not in _UNITS or not number:
            raise ValueError(f"invalid duration: {text!r}")
        position = order.index(ch)
        if position <= last_unit:
            raise ValueError(f"invalid duration: {text!r}")
        last_unit = position
        total += int(number) * _UNITS[ch]
        number = ""
    if number:
        raise ValueError(f"invalid duration: {text!r}")
    return total
