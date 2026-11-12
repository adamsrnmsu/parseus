"""PEAT authentication-enumeration analytic."""



from parseus.analytics.base import Analytic
from parseus.analytics.core import build_ecs_alert, build_ecs_baseline
from parseus.analytics.capabilities import (
    PeatFileSource,
    JoblibArtifact,
    Unsupervised,
)
from parseus.analytics.registry import register_analytic
from parseus.preprocessors.peat_dir import (
    peat_event_transform,
)
from parseus.preprocessors.peat_normalize import (
    event_cause_effect,
    event_is_auth_failure,
    event_timestamp,
)
from parseus.preprocessors.transform import compose
from parseus.util.datetime_utils import datetime_parser


def _collect_auth_failures(events, last_fail, time_diffs, associated):
    """Fold one event log into the running auth-failure gaps and context.

    Appends to ``time_diffs`` / ``associated`` in place and returns the
    timestamp of the most recent timestamped failure.
    """
    for event in events:
        if not event_is_auth_failure(event):
            continue
        event_time = event_timestamp(event)
        if event_time and last_fail:
            time_diffs.append(event_time - last_fail)
        if event_time:
            last_fail = event_time
        cause_effect = event_cause_effect(event)
        if cause_effect and cause_effect not in associated:
            associated.append(cause_effect)
    return last_fail


def _enumeration_alerts(device, pull_time, logs):
    """Alerts for one device when 10+ auth failures arrive under 5s apart."""
    alerts = []
    associated_data = []
    time_diffs = []
    last_fail = None
    for events in logs.values():
        last_fail = _collect_auth_failures(events, last_fail, time_diffs, associated_data)
        short_fails = [
            d.total_seconds() for d in time_diffs if d.total_seconds() < 5
        ]
        if len(short_fails) >= 10:
            alerts.append(
                build_ecs_alert(
                    "possible auth credential enumeration on device",
                    f"Device {device} has potential brute-force authentication enumeration ({len(time_diffs)} failures)",
                    3.0,
                    category=["host"],
                    timestamp=datetime_parser(pull_time),
                    host={
                        "authentication_failures_info": associated_data,
                        "authentication_failures_count": len(short_fails),
                        "authentication_failures_avg_resubmit": sum(short_fails)
                        / len(short_fails),
                        "current_pull": datetime_parser(pull_time),
                    },
                )
            )
    return alerts


@register_analytic(
    key="PeatAuthEnumerationAnalytic",
    family="ot_internals",
    replace=True,
    data_source=PeatFileSource(),
    training=Unsupervised(),
    persistence=JoblibArtifact(),
    fit_idx={"index_name": "/opt/data/"},
    run_idx={"index_name": "/opt/data/"},
    out_idx={"index_name": "alerts-peat_auth_enumeration_analytic"},
)
class PeatAuthEnumerationAnalytic(Analytic):
    """
    ----------------------------------
    # Name: PeatAuthEnumerationAnalytic
    ----------------------------------
    How: Examines PEAT pull event logs for evidence of brute-force enumeration of authentication credentials

    When: You have run a PEAT pull and want to check for patterns of failed authentications

    Usage: <>

    Mitre-Data-Sources:
        + Asset: Asset Inventory

    Mitre-Techniques:
        + Rogue Master (T0848)

    Purdue-Levels:
        + level-1
        + level-2

    Tags:
        + IP
        + Asset Inventory
    """

    def __init__(self, pull_duration=0, verbose=0, **kwargs):
        super().__init__(verbose=verbose, pull_duration=pull_duration, **kwargs)
        self._transform = compose(self._transform, peat_event_transform)
        self.training_device_events_ = {}

    @classmethod
    def requires_fit(cls) -> bool:
        """
        True if the analytic must be fit before being used, or False otherwise.
        This may be set to false if the analytic is Fit and saved before hand.
        """
        return False

    def _fit(self, *peat_docs):
        events = peat_docs[0]
        self.training_device_events_ = events

    def _run(self, start, end, peat_docs, state, **other_docs) -> list:
        device_events = peat_docs

        alerts = []
        for device, (pull_time, logs) in device_events.items():
            alerts.extend(_enumeration_alerts(device, pull_time, logs))

        baseline_alerts = [
            build_ecs_baseline(
                f"Device {device} has event logs: {logs}",
                category=["host"],
                timestamp=datetime_parser(pull_time),
                host={"events": list(logs.items())},
            )
            for device, (pull_time, logs) in device_events.items()
        ]
        return alerts + baseline_alerts
