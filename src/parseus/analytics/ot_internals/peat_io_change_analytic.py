"""PEAT device I/O module-change analytic."""
import json

from deepdiff import DeepDiff

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
    peat_io_transform,
)
from parseus.preprocessors.transform import compose


def _diff_module_type(module_type, current, previous, added, removed, changed):
    """Append this category's module differences to the shared diff lists.

    ``current``/``previous`` map module name -> configuration.
    """
    for name, config in current.items():
        label = module_type + ": " + name
        if name not in previous:
            added.append(label)
        elif config != previous[name]:
            diff = DeepDiff(previous[name], config, ignore_order=True)
            changed.append((label, json.loads(diff.to_json())))
    removed.extend(module_type + ": " + name for name in previous if name not in current)


def _io_alerts(device, current, previous):
    """Alerts for one device; diffs accumulate across module categories.

    ``current``/``previous`` are ``(timestamp, {category: {module: config}})``.
    """
    alerts = []
    added, removed, changed = [], [], []
    for module_type, modules in current[1].items():
        # Analytic ignores changes to entire categories of modules for now (in/out)
        if module_type not in previous[1]:
            continue
        _diff_module_type(
            module_type, modules, previous[1][module_type], added, removed, changed
        )
        if added or removed or changed:
            alerts.append(
                build_ecs_alert(
                    "I/O modules changed on device",
                    f"Device {device} has changed I/O modules: {added} were added, {removed} were removed, {changed} were changed",
                    3.0,
                    category=["host"],
                    timestamp=datetime_parser(current[0]),
                    host={
                        "io_modules_added": added,
                        "io_modules_removed": removed,
                        "io_modules_changed": changed,
                        "current_pull": datetime_parser(current[0]),
                        "previous_pull": datetime_parser(previous[0]),
                    },
                )
            )
    return alerts


@register_analytic(
    key="PeatIOChangeAnalytic",
    family="ot_internals",
    replace=True,
    data_source=PeatFileSource(),
    training=Supervised(),
    persistence=JoblibArtifact(),
    fit_idx={"index_name": "/opt/data/"},
    run_idx={"index_name": "/opt/data/"},
    out_idx={"index_name": "alerts-peat_io_change_analytic"},
)
class PeatIOChangeAnalytic(Analytic):
    """
    ----------------------------------
    # Name: PeatIOChangeAnalytic
    ----------------------------------
    How: Compares PEAT pull results for changes in device IO state configuration

    When: You have run PEAT pull previously and want to identify changes in device IO configuration settings

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
        self._transform = compose(self._transform, peat_io_transform)
        self.training_device_io_ = {}

    @classmethod
    def requires_fit(cls) -> bool:
        """
        True if the analytic must be fit before being used, or False otherwise.
        This may be set to false if the analytic is Fit and saved before hand.
        """
        return True

    def _fit(self, *peat_docs):
        io = peat_docs[0]
        self.training_device_io_ = io

    def _run(self, start, end, peat_docs, state, **other_docs) -> list:
        alerts = []
        for device, current in peat_docs.items():
            if device in self.training_device_io_:
                alerts.extend(
                    _io_alerts(device, current, self.training_device_io_[device])
                )
        return alerts

    def get_fit_results(self):
        """
        Baseline training information stored for each device detected
        by peat. Devices are identified by PEAT summary identity fields.
        """
        # Display dictionary as table
        # Omit process mem dumps
        training_dicts = [
            {
                "device_id": device[0],
                "@timestamp": datetime_parser(device[1][0]),
                "io_modules": list(
                    (module_category, device[1][1][module_category])
                    for module_category in device[1][1]
                ),
            }
            for device in self.training_device_io_.items()
        ]
        return convert_to_df(training_dicts)
