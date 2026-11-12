"""PEAT device-inventory change analytic."""

from datetime import datetime, timezone

import pandas as pd

from parseus.analytics.base import Analytic
from parseus.analytics.capabilities import (
    PeatFileSource,
    JoblibArtifact,
    Supervised,
)
from parseus.analytics.core import build_ecs_alert
from parseus.analytics.ot_internals._peat_common import convert_to_df
from parseus.analytics.registry import register_analytic
from parseus.preprocessors.peat_dir import peat_device_transform
from parseus.preprocessors.transform import compose


@register_analytic(
    key="PeatDeviceChangeAnalytic",
    family="ot_internals",
    replace=True,
    data_source=PeatFileSource(),
    training=Supervised(),
    persistence=JoblibArtifact(),
    fit_idx={"index_name": "/opt/data/"},
    run_idx={"index_name": "/opt/data/"},
    out_idx={"index_name": "alerts-peat_device_change_analytic"},
)
class PeatDeviceChangeAnalytic(Analytic):
    """
    ----------------------------------
    # Name: PeatDeviceChangeAnalytic
    ----------------------------------
    How: Compares PEAT result directories for changes in device identities

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
        self._transform = compose(self._transform, peat_device_transform)
        self.training_device_set_ = set()
        self.predict_results_doc = """
        Baseline for each device detected by a peat scan.
        Alerts for each new device detected by the second peat scan.
        Alerts also for each device detected by the training peat scan
        but not detected on the second scan.
        """
        self.fit_results_doc = """
        Baseline training information stored for each device detected
        by peat. Devices are identified by PEAT summary identity fields.
        """

    @classmethod
    def requires_fit(cls) -> bool:
        """
        True if the analytic must be fit before being used, or False otherwise.
        This may be set to false if the analytic is Fit and saved before hand.
        """
        return True

    def _fit(self, *peat_docs):
        devices = peat_docs
        for peat_doc in devices:
            self.training_device_set_.update(peat_doc)

    def _run(self, start, end, peat_docs, state, **other_docs) -> list:
        devices = peat_docs

        pre_dict = [
            {
                "device_id": device_id,
                "pre_time": datetime.fromtimestamp(timestamp, tz=timezone.utc),
            }
            for device_id, timestamp in self.training_device_set_
        ]
        post_dict = [
            {
                "device_id": device_id,
                "post_time": datetime.fromtimestamp(timestamp, tz=timezone.utc),
            }
            for device_id, timestamp in devices
        ]

        pre_df = pd.DataFrame(pre_dict, columns=["device_id", "pre_time"])
        post_df = pd.DataFrame(post_dict, columns=["device_id", "post_time"])

        combined_df = (
            pd.concat([pre_df, post_df])
            .groupby("device_id", as_index=False)
            .aggregate({"pre_time": "first", "post_time": "last"})
            .set_index("device_id")
        )

        alerts = []
        event_timestamp = datetime.now(timezone.utc)

        # Alerts where a device is present in the peat pull being compared but
        # not on the trained peat pull.
        for index, row in combined_df[combined_df["pre_time"].isnull()].iterrows():
            alerts.append(
                build_ecs_alert(
                    "new-device",
                    f"New Device: {index}",
                    1.0,
                    category="host",
                    timestamp=row["post_time"],
                    event_extra={"created": event_timestamp},
                )
            )

        # Alerts where a device was present in a trained peat pull but not
        # on the peat pull being compared.
        for index, row in combined_df[combined_df["post_time"].isnull()].iterrows():
            alerts.append(
                build_ecs_alert(
                    "missing-device",
                    f"Missing Device: {index}",
                    1.0,
                    category="host",
                    timestamp=row["pre_time"],
                    event_extra={"created": event_timestamp},
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
                "device_id": device_id,
                "@timestamp": datetime.fromtimestamp(timestamp, tz=timezone.utc),
            }
            for device_id, timestamp in self.training_device_set_
        ]
        return convert_to_df(training_dicts)
