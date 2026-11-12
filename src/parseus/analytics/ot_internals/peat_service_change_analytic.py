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

    When: You have run PEAT pull previously and want to identify changes in discovered devices

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
        device_services = peat_docs
        alerts = []

        for device in device_services:
            if device not in self.training_device_services_:
                continue
            diff_services_added = []
            diff_services_removed = []
            for service in device_services[device][1]:
                if service not in self.training_device_services_[device][1]:
                    diff_services_added.append(service)

            for service in self.training_device_services_[device][1]:
                if service not in device_services[device][1]:
                    diff_services_removed.append(service)

            protocols_shared = set(
                [service["protocol"] for service in device_services[device][1]]
            ).intersection(
                set(
                    [
                        service["protocol"]
                        for service in self.training_device_services_[device][1]
                    ]
                )
            )

            # Highlight changed ports or open/close services
            # TODO: generate alerts for changed TCP/UDP transport protocol?
            for protocol in protocols_shared:
                # Alert on changed ports
                ports_added = [
                    service["port"]
                    for service in device_services[device][1]
                    if service["protocol"] == protocol
                ]
                ports_removed = [
                    service["port"]
                    for service in self.training_device_services_[device][1]
                    if service["protocol"] == protocol
                ]

                if set(ports_added) != set(ports_removed):
                    alerts.append(
                        build_ecs_alert(
                            "service changed on device",
                            f"Device {device} has changed ports for protocol {protocol}: changed from ports {ports_removed} to ports {ports_added}",
                            3.0,
                            category=["host"],
                            timestamp=datetime_parser(device_services[device][0]),
                            host={
                                "device": device,
                                "protocol": protocol,
                                "ports_old": ports_removed,
                                "ports_new": ports_added,
                                "current_pull": datetime_parser(
                                    device_services[device][0]
                                ),
                                "previous_pull": datetime_parser(
                                    self.training_device_services_[device][0]
                                ),
                            },
                        )
                    )

                # Alert on a service becoming available/unavailable
                status_added = [
                    (service["status"], service["port"])
                    for service in device_services[device][1]
                    if service["protocol"] == protocol
                ]
                status_removed = [
                    (service["status"], service["port"])
                    for service in self.training_device_services_[device][1]
                    if service["protocol"] == protocol
                ]

                if set([status[0] for status in status_added]) != set(
                    [status[0] for status in status_removed]
                ):
                    alerts.append(
                        build_ecs_alert(
                            "service changed on device",
                            f"Device {device} has changed open/closed status for protocol {protocol} on the following ports: changed from status {status_removed} to status {status_added}",
                            3.0,
                            category=["host"],
                            timestamp=datetime_parser(device_services[device][0]),
                            host={
                                "device": device,
                                "protocol": protocol,
                                "status_old": status_removed,
                                "status_new": status_added,
                                "current_pull": datetime_parser(
                                    device_services[device][0]
                                ),
                                "previous_pull": datetime_parser(
                                    self.training_device_services_[device][0]
                                ),
                            },
                        )
                    )

            # Simple diff to compare service ports, availability, and transport protocols
            # Preserving all info for postprocessing
            if diff_services_added or diff_services_removed:
                alerts.append(
                    build_ecs_alert(
                        "service changed on device",
                        f"Device {device} has changed services: {diff_services_added} were added, {diff_services_removed} were removed",
                        3.0,
                        category=["host"],
                        timestamp=datetime_parser(device_services[device][0]),
                        host={
                            "services_added": diff_services_added,
                            "service_removed": diff_services_removed,
                            "current_pull": datetime_parser(device_services[device][0]),
                            "previous_pull": datetime_parser(
                                self.training_device_services_[device][0]
                            ),
                        },
                    )
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
