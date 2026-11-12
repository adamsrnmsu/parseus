"""
Analytic for SCPI data as ingested by the GPIB ingestor (see
parseus.ingestors.gpib.parser) into the "scpi_parsed-*" index.
Looks for SCPI command codes added or removed relative to a baseline.
"""

from copy import deepcopy
from datetime import datetime, timezone

import pandas as pd

from parseus.analytics.base import Analytic
from parseus.analytics.core import build_ecs_alert
from parseus.analytics.core.suppression import SuppressionWindow, window_now
from parseus.analytics.capabilities import (
    ElasticsearchSource,
    JoblibArtifact,
    Supervised,
)
from parseus.analytics.registry import register_analytic


@register_analytic(
    key="ScpiCompareAnalytic",
    family="scpi_analytics",
    replace=True,
    data_source=ElasticsearchSource(),
    training=Supervised(),
    persistence=JoblibArtifact(),
    fit_idx={"index_name": "ground_truth*"},
    run_idx={"index_name": "scpi_parsed*"},
    out_idx={"index_name": "alerts-scpi_analytic"},
)
class ScpiCompareAnalytic(Analytic):
    """
    Name: ScpiCompareAnalytic

    How: Compares the set of SCPI command codes seen during current operation
         against a baseline day of normal operation, reporting command codes
         that were added or removed. The comparison is membership-based and
         ignores command order; the baseline day with the fewest differences is
         chosen for reporting.

    When: Detect end device or SCPI communication that might be compromised.

    Mitre-Data-Sources:
        + Network Traffic: Network Traffic Content

    Mitre-Techniques:
        + impair-process-control (TA0106)
        + inhibit-response-function (TA0107)
        + command-and-control (TA0101)
        + Command: Command Execution
        + Device Restart/Shutdown (T0816)
        + Change Operating Mode (T0858)
        + Change Program State (T0875)
        + Rogue Master (T0848)

    Purdue-Levels:
        + level 1
    """

    def __init__(self, pull_duration=0, **kwargs):
        super().__init__(pull_duration=pull_duration, **kwargs)
        self._transform = scpi_transform
        self.fit_results_doc = "Baseline SCPI command codes grouped by day."
        self.run_results_doc = "Alerts when SCPI command codes deviate from baseline."
        self.ground_truth_ = []

    def _fit(self, training_documents):
        "Creates a list of list of documents, where each sublist is a day"
        self.ground_truth_ = split_days(training_documents)
        return training_documents

    def get_fit_results(self):
        """
        Formats baseline data as a pandas dataframe.
        """
        return pd.DataFrame(self.ground_truth_)

    def _run(self, start, end, run_docs, state, **other_docs):
        """
        Attempts to find the day of training data which has the least difference from our current run_docs.
        Returns the list of alerts produced by that comparison.

        Each distinct (func_code, added/removed) finding is gated by an
        alert-suppression window: the GPIB listener is a passive, promiscuous
        bus listener, so routine repeat traffic keeps producing genuinely new
        documents even when nothing operator-relevant changed, which would
        otherwise re-alert the same already-reported deviation on every run
        that happens to see it again. The default suppression window ("0s")
        never suppresses, so behaviour is unchanged until an operator
        configures a window.
        """
        suppression = SuppressionWindow(state, self.suppression_)
        now = window_now(end)
        list_of_alerts = search_based_compare(
            self.ground_truth_, run_docs, suppression=suppression, now=now
        )
        return get_shortest_list(list_of_alerts)


def get_shortest_list(list_of_lists):
    "Takes a list of lists of dicts and returns the shortest sublist"
    _, i = min((len(sub_list), i) for i, sub_list in enumerate(list_of_lists))
    shortest_sub_list = list_of_lists[i]
    return shortest_sub_list


def search_based_compare(fit_dataset, run_docs, suppression=None, now=None):
    """
    For each sublist in the fit_dataset, create a list of alerts
    for additions/removals when comparing it to the run_docs
    """
    list_of_alerts = []
    for ground_truth_list in fit_dataset:
        # Find all of the func_codes in the run_docs that were in the ground_truth_list,
        # change them to None
        run_docs_copy = deepcopy(run_docs)
        for ground_truth_doc in ground_truth_list:
            for run_doc_index, run_doc in enumerate(run_docs_copy):
                if ground_truth_doc["func_code"] == run_doc["func_code"]:
                    run_docs_copy[run_doc_index]["func_code"] = None
                    break

        # Find all of the func_codes in the ground_truth that were also in the run_docs,
        # change them to None
        ground_truth_copy = deepcopy(ground_truth_list)
        for run_doc in run_docs:
            for ground_truth_index, ground_truth_doc in enumerate(ground_truth_copy):
                if run_doc["func_code"] == ground_truth_doc["func_code"]:
                    ground_truth_copy[ground_truth_index]["func_code"] = None
                    break

        # Create alerts based on the remaining entries
        alerts = search_based_make_alerts(
            ground_truth_copy, run_docs_copy, suppression=suppression, now=now
        )
        list_of_alerts.append(alerts)
    return list_of_alerts


def search_based_make_alerts(fit_docs, run_docs, suppression=None, now=None):
    """
    Returns alert dicts for all func_codes added or removed between the fit and run data,
    without respect for order.
    """
    comparison_timestamp = fit_docs[0]["@timestamp"] if fit_docs else datetime.now(timezone.utc)
    run_data_timestamp = run_docs[0]["@timestamp"] if run_docs else datetime.now(timezone.utc)
    alerts = []
    for value_removed in fit_docs:
        func_code = value_removed["func_code"]
        if func_code and (
            suppression is None
            or suppression.should_emit(func_code, "Value Removed", now=now)
        ):
            alerts.append(
                build_ecs_alert(
                    action="Value Removed",
                    original=f"Detected value removed: {func_code}",
                    riskscore=5.0,
                    category="network",
                    kind="alert",
                    timestamp=run_data_timestamp,
                    event_extra={"ground_truth_date": comparison_timestamp},
                    handled=False,
                )
            )
    for value_added in run_docs:
        func_code = value_added["func_code"]
        if func_code and (
            suppression is None
            or suppression.should_emit(func_code, "Value Added", now=now)
        ):
            current_time = value_added["@timestamp"]
            alerts.append(
                build_ecs_alert(
                    action="Value Added",
                    original=f"Detected added value: {func_code}",
                    riskscore=5.0,
                    category="network",
                    kind="alert",
                    timestamp=current_time,
                    event_extra={"ground_truth_date": comparison_timestamp},
                    handled=False,
                )
            )
    return alerts


def split_days(dicts_with_timestamp):
    """
    Takes a list of dicts that have '@timestamp' fields
    Returns a list of lists of dicts that are separated by day.
    """
    list_of_day_lists = []
    current_day_list = []
    for doc in dicts_with_timestamp:
        day = doc["@timestamp"].day
        if not current_day_list:  # first time
            current_day = day
        if current_day != day:  # new day
            list_of_day_lists.append(current_day_list)
            current_day_list = []
            current_day = day
        current_day_list.append(doc)
    list_of_day_lists.append(current_day_list)
    return list_of_day_lists


def scpi_transform(docs, **kwargs):
    """
    Takes result from datastore query and returns a dictionary
    with the fields used by the analytic.
    """
    scpi_commands = []
    for doc in docs:
        scpi_command = {}
        scpi_command["func_code"] = doc["data"]
        scpi_command["@timestamp"] = datetime.fromisoformat(doc["@timestamp"])
        scpi_commands.append(scpi_command)
    return scpi_commands
