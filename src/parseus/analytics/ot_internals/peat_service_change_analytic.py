"""PEAT device service-change analytic."""

from parseus.analytics.base import Analytic
from parseus.analytics.core import build_ecs_alert
from parseus.analytics.capabilities import (
    PeatFileSource,
    JoblibArtifact,
    Supervised,
)
from parseus.analytics.ot_internals._peat_common import convert_to_df
from parseus.analytics.registry import register_analytic
from parseus.util.datetime_utils import datetime_parser
from parseus.preprocessors.peat_dir import (
    peat_service_transform,
)
from parseus.preprocessors.transform import compose


def _ports(services, protocol):
    return [s["port"] for s in services if s["protocol"] == protocol]


def _statuses(services, protocol):
    return [(s["status"], s["port"]) for s in services if s["protocol"] == protocol]


def _service_alert(current, previous, description, host):
    """Build a service-change alert carrying the current/previous pull times."""
    return build_ecs_alert(
        "service changed on device",
        description,
        3.0,
        category=["host"],
        timestamp=datetime_parser(current[0]),
        host={
            **host,
            "current_pull": datetime_parser(current[0]),
            "previous_pull": datetime_parser(previous[0]),
        },
    )


def _port_change_alert(device, protocol, current, previous):
    ports_added = _ports(current[1], protocol)
    ports_removed = _ports(previous[1], protocol)
    if set(ports_added) == set(ports_removed):
        return None
    return _service_alert(
        current,
        previous,
        f"Device {device} has changed ports for protocol {protocol}: changed from ports {ports_removed} to ports {ports_added}",
        {
            "device": device,
            "protocol": protocol,
            "ports_old": ports_removed,
            "ports_new": ports_added,
        },
    )


def _status_change_alert(device, protocol, current, previous):
    status_added = _statuses(current[1], protocol)
    status_removed = _statuses(previous[1], protocol)
    if {s[0] for s in status_added} == {s[0] for s in status_removed}:
        return None
    return _service_alert(
        current,
        previous,
        f"Device {device} has changed open/closed status for protocol {protocol} on the following ports: changed from status {status_removed} to status {status_added}",
        {
            "device": device,
            "protocol": protocol,
            "status_old": status_removed,
            "status_new": status_added,
        },
    )


def _service_alerts(device, current, previous):
    """Alerts for one device: per-protocol port/status changes, then the service diff.

    ``current``/``previous`` are ``(timestamp, [service dict, ...])`` pulls.
    """
    alerts = []
    # TODO: generate alerts for changed TCP/UDP transport protocol?
    protocols_shared = {s["protocol"] for s in current[1]} & {
        s["protocol"] for s in previous[1]
    }
    for protocol in protocols_shared:
        for build in (_port_change_alert, _status_change_alert):
            alert = build(device, protocol, current, previous)
            if alert:
                alerts.append(alert)

    # Simple diff to compare service ports, availability, and transport protocols
    # Preserving all info for postprocessing
    added = [s for s in current[1] if s not in previous[1]]
    removed = [s for s in previous[1] if s not in current[1]]
    if added or removed:
        alerts.append(
            _service_alert(
                current,
                previous,
                f"Device {device} has changed services: {added} were added, {removed} were removed",
                {"services_added": added, "service_removed": removed},
            )
        )
    return alerts


@register_analytic(
    key="PeatServiceChangeAnalytic",
    family="ot_internals",
    replace=True,
    data_source=PeatFileSource(),
    training=Supervised(),
    persistence=JoblibArtifact(),
    fit_idx={"index_name": "/opt/data/"},
    run_idx={"index_name": "/opt/data/"},
    out_idx={"index_name": "alerts-peat_service_change_analytic"},
)
class PeatServiceChangeAnalytic(Analytic):
    """
    ----------------------------------
    # Name: PeatServiceChangeAnalytic
    ----------------------------------
    How: Compares PEAT result directories for changes in the names, open ports, and running/inactive status of device services

    When: You have run PEAT pull previously and want to identify changes in discoved devices

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
        self._transform = compose(self._transform, peat_service_transform)
        self.training_device_services_ = {}

    @classmethod
    def requires_fit(cls) -> bool:
        """
        True if the analytic must be fit before being used, or False otherwise.
        This may be set to false if the analytic is Fit and saved before hand.
        """
        return True

    def _fit(self, *peat_docs):
        device_services = peat_docs[0]
        self.training_device_services_ = device_services

    def _run(self, start, end, peat_docs, state, **other_docs) -> list:
        alerts = []
        for device, current in peat_docs.items():
            if device in self.training_device_services_:
                alerts.extend(
                    _service_alerts(device, current, self.training_device_services_[device])
                )
        return alerts

    def get_fit_results(self):
        """
        Baseline training information stored for each device detected
        by peat. Devices are identified by PEAT summary identity fields.
        """
        # Display dictionary as table
        training_dicts = [
            {
                "device_id": device[0],
                "@timestamp": datetime_parser(device[1][0]),
                "services": list(device[1][1]),
            }
            for device in self.training_device_services_.items()
        ]
        return convert_to_df(training_dicts)
