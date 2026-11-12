from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.db import models


DAY_CHOICES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SYSMON_ANALYTIC_CLASS_TO_RULE_TYPE = {
    "SysmonUnapprovedUserAnalytic": "user",
    "SysmonUnapprovedIpAnalytic": "ip",
    "SysmonUnapprovedDeviceAnalytic": "device",
}


def _window_error(message: str) -> ValidationError:
    return ValidationError({"allowed_windows": message})


def _validate_window(number: int, window: object) -> None:
    """Raise ValidationError if one allowed window (1-based ``number``) is bad."""
    if not isinstance(window, dict):
        raise _window_error(f"Window {number} must be an object.")

    days = window.get("days", [])
    if not isinstance(days, list):
        raise _window_error(f"Window {number} days must be a list.")
    invalid_days = [day for day in days if str(day).lower() not in DAY_CHOICES]
    if invalid_days:
        raise _window_error(
            f"Window {number} has invalid day(s): "
            f"{', '.join(map(str, invalid_days))}. Use {', '.join(DAY_CHOICES)}."
        )

    for field in ("start", "end"):
        if field not in window:
            raise _window_error(f"Window {number} missing {field}.")
        try:
            time.fromisoformat(str(window[field]))
        except ValueError as err:
            raise _window_error(
                f"Window {number} {field} must use HH:MM or HH:MM:SS format."
            ) from err


class SysmonRule(models.Model):
    """Allow-list and schedule rule for Sysmon host anomaly analytics."""

    USER = "user"
    IP = "ip"
    DEVICE = "device"
    ANALYTIC_TYPE_CHOICES = [
        (USER, "Sysmon User"),
        (IP, "Sysmon IP"),
        (DEVICE, "Sysmon Device"),
    ]

    name = models.CharField(max_length=150)
    analytic_type = models.CharField(max_length=20, choices=ANALYTIC_TYPE_CHOICES)
    match_value = models.CharField(
        max_length=500,
        help_text=(
            "User name, source IP, or device ID to allow. Matching is "
            "case-insensitive."
        ),
    )
    enabled = models.BooleanField(default=True)
    timezone = models.CharField(max_length=64, default="UTC")
    allowed_windows = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "List of allowed windows. Example: "
            "[{\"days\":[\"mon\",\"thu\",\"fri\"],"
            "\"start\":\"09:00\",\"end\":\"17:00\"}]. "
            "Leave empty to allow the value at any time."
        ),
    )
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("analytic_type", "match_value", "name")
        indexes = [
            models.Index(fields=["analytic_type", "enabled"]),
            models.Index(fields=["match_value"]),
        ]

    def __str__(self):
        return f"{self.get_analytic_type_display()}: {self.match_value} ({self.name})"

    def clean(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as err:
            raise ValidationError({"timezone": "Unknown timezone."}) from err

        if not isinstance(self.allowed_windows, list):
            raise ValidationError(
                {"allowed_windows": "Allowed windows must be a JSON list."}
            )

        for index, window in enumerate(self.allowed_windows):
            _validate_window(index + 1, window)

    def to_analytic_rule(self) -> dict[str, object]:
        """Return the JSON-serializable rule shape consumed by analytics."""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "values": [self.match_value],
            "timezone": self.timezone,
            "allowed_windows": self.allowed_windows or [],
        }


def get_sysmon_rules_for_analytic_class(
    analytic_class_name: str,
) -> list[dict[str, object]]:
    """Return enabled UI-managed Sysmon rules for an analytic class name."""
    class_name = str(analytic_class_name or "").split(".")[-1]
    analytic_type = SYSMON_ANALYTIC_CLASS_TO_RULE_TYPE.get(class_name)
    if not analytic_type:
        return []

    return [
        rule.to_analytic_rule()
        for rule in SysmonRule.objects.filter(
            analytic_type=analytic_type,
            enabled=True,
        )
    ]
