"""Shared allow-list rule helpers for host anomaly analytics."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from parseus.analytics.core.suppression import window_now

DAY_ALIASES = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


def normalize_rule_value(value) -> str:
    """Normalize a rule subject value for comparison."""
    return str(value or "").strip().casefold()


def parse_event_datetime(value) -> datetime:
    """Parse datastore timestamps into a timezone-aware datetime."""
    if isinstance(value, datetime):
        parsed = value
    else:
        timestamp = str(value or "").strip()
        if timestamp.endswith("Z"):
            timestamp = f"{timestamp[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError:
            parsed = datetime.now(tz=ZoneInfo("UTC"))

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed


def parse_time(value: str) -> time:
    """Parse a HH:MM or HH:MM:SS time string."""
    return time.fromisoformat(str(value).strip())


def day_to_number(day) -> int | None:
    """Return Python weekday number for a day label or integer."""
    if isinstance(day, int):
        return day if 0 <= day <= 6 else None
    return DAY_ALIASES.get(str(day or "").strip().lower())


def get_rule_values(rule: dict, value_keys: tuple[str, ...]) -> set[str]:
    """Return normalized subject values from a rule."""
    values = []
    for key in ("values", *value_keys):
        raw_values = rule.get(key, [])
        if isinstance(raw_values, str):
            raw_values = [raw_values]
        values.extend(raw_values or [])
    return {normalize_rule_value(value) for value in values if str(value or "").strip()}


def rule_matches_value(rule: dict, value: str, value_keys: tuple[str, ...]) -> bool:
    """Return whether a rule applies to the provided subject value."""
    return normalize_rule_value(value) in get_rule_values(rule, value_keys)


def timestamp_in_window(timestamp: datetime, window: dict) -> bool:
    """Return whether a timestamp falls within an allowed time window."""
    days = window.get("days", [])
    allowed_days = {day_to_number(day) for day in days}
    allowed_days.discard(None)
    if allowed_days and timestamp.weekday() not in allowed_days:
        return False

    start = parse_time(window.get("start", "00:00"))
    end = parse_time(window.get("end", "23:59:59"))
    current_time = timestamp.time().replace(tzinfo=None)

    if start <= end:
        return start <= current_time <= end

    # Overnight window, e.g. 22:00-06:00.
    return current_time >= start or current_time <= end


def timestamp_allowed_by_rule(timestamp_value, rule: dict, default_timezone="UTC") -> bool:
    """Return whether a timestamp is inside any allowed window for a rule."""
    windows = rule.get("allowed_windows") or []
    if not windows:
        # A matching enabled rule with no windows means always allowed.
        return True

    timezone_name = rule.get("timezone") or default_timezone or "UTC"
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")

    timestamp = parse_event_datetime(timestamp_value).astimezone(timezone)
    return any(timestamp_in_window(timestamp, window) for window in windows)


def evaluate_allow_rules(
    value: str,
    timestamp,
    rules: list[dict] | None,
    value_keys: tuple[str, ...] = (),
    default_timezone="UTC",
) -> tuple[bool | None, dict | None, str | None]:
    """Evaluate rules for a subject value.

    Returns (allowed, matching_rule, reason). ``allowed`` is None when no rule
    matches the value, True when a matching rule permits the event, and False
    when a matching rule denies the event.
    """
    for rule in rules or []:
        if rule.get("enabled", True) is False:
            continue
        if not rule_matches_value(rule, value, value_keys):
            continue
        if timestamp_allowed_by_rule(timestamp, rule, default_timezone):
            return True, rule, None
        return False, rule, "outside_allowed_window"

    return None, None, None


def describe_allowed_windows(rule: dict | None) -> str:
    """Return a human-readable summary of a rule's allowed windows."""
    windows = (rule or {}).get("allowed_windows") or []
    if not windows:
        return "always"

    descriptions = []
    for window in windows:
        days = ", ".join(window.get("days") or ["every day"])
        descriptions.append(
            f"{days} {window.get('start', '00:00')}-{window.get('end', '23:59:59')}"
        )
    return "; ".join(descriptions)


def alert_rule_context(
    value,
    timestamp,
    rules: list[dict] | None,
    known_good: set,
    value_keys: tuple[str, ...],
    default_timezone="UTC",
    window=None,
    now=None,
) -> tuple[dict | None, dict] | None:
    """Decide whether an allow-list subject warrants an alert.

    Returns None when the value is allowed (by a rule or the known-good set) or
    suppressed by ``window``. Otherwise returns ``(rule, rule_fields)``: ``rule``
    is the denying rule (None when the value simply is not on the allow-list) and
    ``rule_fields`` is the ``rule`` section of the alert.
    """
    allowed, rule, reason = evaluate_allow_rules(
        value, timestamp, rules, value_keys=value_keys, default_timezone=default_timezone
    )
    if allowed is True or (allowed is None and value in known_good):
        return None
    # Suppression is keyed on the run clock: with the default "0s" it always
    # emits; a configured window re-alerts a given value at most once per window.
    if window is not None and not window.should_emit(value, now=window_now(now)):
        return None
    if reason == "outside_allowed_window":
        name = (rule or {}).get("name", "unnamed rule")
        return rule, {"name": name, "reason": reason}
    return rule, {"reason": "not_in_whitelist"}
