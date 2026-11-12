"""
OT Function Code Analytics
"""

from collections import defaultdict
from functools import partial

import pandas as pd

from parseus.analytics.base import Analytic, OutsideStdDevModel
from parseus.analytics.core import build_ecs_alert
from parseus.analytics.core.suppression import SuppressionWindow, window_now
from parseus.analytics.capabilities import (
    ElasticsearchSource,
    IndexQuery,
    JoblibArtifact,
    Supervised,
)
from parseus.analytics.registry import register_analytic
from parseus.util.datetime_utils import datetime_parser, make_zulu_time


def function_code_transform(docs, protocols, **kwargs):
    """
    Expects the docs in the format:
    {
        "key": {"date": <timestamp>}
        "source_ips": {
            "buckets": [
                {
                    "destination_ips": {
                        "buckets": [
                            {
                                <OT_protocol_str>: {  <---- This string is "dnp3", "modbus", etc.
                                    "buckets": [
                                        {
                                            "key": <OT_func_code_str>
                                            "doc_count": <num_of_codes_str>
                                        }
                                        ...
                                    ]
                                }
                            }
                            ...
                        ]
                    }
                }
                ...
            ]
        }
    }
    """

    # Time-bucketed function code data in format:
    # [
    # {"@timestamp": time, "<source>_<destination>:<protocol>:<code>": count, "<src_dst_proto_func>": count_2, ...},
    # { time bucket 2}, ...
    # ]
    function_code_data = []
    for time_bucket in docs:
        time = time_bucket["key"]["date"]
        row = {"@timestamp": time}
        for src_ip_item in time_bucket["source_ips"]["buckets"]:
            source = src_ip_item["key"]
            for dst_ip_item in src_ip_item["destination_ips"]["buckets"]:
                destination = dst_ip_item["key"]
                for protocol in protocols:
                    # Placeholder dict so that ot_func_item processing works for
                    # protocols not found in data
                    if protocol not in dst_ip_item:
                        dst_ip_item[protocol] = dict()
                        dst_ip_item[protocol]["buckets"] = list()
                    if protocol in ["selfm_func_code"]:  # SELFM has no nesting
                        continue
                    for ot_func_item in dst_ip_item[protocol]["buckets"]:
                        code = ot_func_item["key"]
                        count = ot_func_item["doc_count"]
                        row[f"{source}_{destination}:{protocol}:{code}"] = count
        function_code_data.append(row)

    df_final = pd.DataFrame.from_dict(function_code_data).fillna(0)
    if df_final.empty:
        return df_final
    df_final["@timestamp"] = df_final["@timestamp"].apply(
        lambda x: make_zulu_time(datetime_parser(x))
    )
    return df_final.set_index("@timestamp")


_RESTART_CODES = {"cold restart", "warm restart", "stop application"}


class _AlertRun:
    """Alerts built so far in one run, plus which finding keys they already cover."""

    def __init__(self, suppression, now, end):
        self.suppression = suppression
        self.now = now
        self.end = end
        self.covered = set()
        self.alerts = []

    def skip(self, key):
        """True if ``key`` was alerted this run or is inside its suppression window."""
        return key in self.covered or self.suppression.already_suppressed(
            key, now=self.now
        )

    def emit(self, alert, keys):
        """Add ``alert`` and mark each of ``keys`` covered and suppressed."""
        self.alerts.append(alert)
        for key in keys:
            self.covered.add(key)
            self.suppression.record(key, now=self.now)


def _first_active_time(df, column):
    """Timestamp of the first row where ``column`` is non-zero."""
    return datetime_parser(f"{df.loc[df[column] != 0].index[0]}")


def _ot_alert(run, action, original, ips, protocol, field, value, timestamp):
    source, destination = ips.split("_")[0], ips.split("_")[1]
    return build_ecs_alert(
        action=action,
        original=original,
        riskscore=5.0,
        category=["network"],
        kind="alert",
        timestamp=timestamp,
        event_extra={"time": run.end},
        source={"ip": source},
        network={"source": source, "destination": destination},
        **{protocol: {field: value}},
    )


def _dos_alerts(run, dataset, hours):
    """Condense Cold Restart/Warm Restart/Stop Application bursts into DoS alerts."""
    # TODO: Heuristic for detecting DoS attacks. Needs new model approach? Still want to alert if high-frequency and new/same as baseline
    # Alert on entire ips/code column, like with anomalous high codes, or on individual time buckets?
    # Currently alerting on entire column, leaving code structure to switch to hourly intervals in the future
    for ips_and_code in dataset.columns:
        if run.skip(ips_and_code):
            continue
        ips, protocol, code = ips_and_code.split(":")
        if code.lower() not in _RESTART_CODES:
            continue
        if any(count >= 30.0 * hours for count in dataset[ips_and_code]):
            timestamp = _first_active_time(dataset, ips_and_code)
            alert = _ot_alert(
                run,
                "possible denial-of-service",
                f"Possible Denial-Of-Service from {protocol}:{code} in {ips} starting at {timestamp}",
                ips,
                protocol,
                "function_code",
                code,
                timestamp,
            )
            run.emit(alert, [ips_and_code])


def _enumeration_alerts(run, dataset, new_func_codes):
    """Condense 10+ new function codes per src/dst/protocol into one enumeration alert."""
    new_codes = defaultdict(list)
    for ips_and_code in new_func_codes:
        if run.skip(ips_and_code):
            continue
        ips, protocol, code = ips_and_code.split(":")
        new_codes[ips + ":" + protocol].append(code)
    for ips_protocol_new, func_codes in new_codes.items():
        # TODO: Heuristic for detecting enumeration attack. Needs fine-tuning - use std dev model?
        if len(func_codes) < 10:
            continue
        ips, protocol = ips_protocol_new.split(":")
        alert = _ot_alert(
            run,
            "possible function code enumeration",
            f"Possible Function Code Enumeration over {protocol} on {ips}: {func_codes}",
            ips,
            protocol,
            "function_codes",
            func_codes,
            _first_active_time(dataset, ips_protocol_new + ":" + func_codes[0]),
        )
        run.emit(alert, [ips_protocol_new + ":" + code for code in func_codes])


def _new_code_alerts(run, dataset, new_func_codes):
    """Alert on each function code not seen in the baseline."""
    for ips_and_code in new_func_codes:
        if run.skip(ips_and_code):
            continue
        ips, protocol, code = ips_and_code.split(":")
        alert = _ot_alert(
            run,
            "new function code",
            f"New Function Codes {protocol}:{code} on {ips}",
            ips,
            protocol,
            "function_code",
            code,
            _first_active_time(dataset, ips_and_code),
        )
        run.emit(alert, [ips_and_code])


def _rate_alerts(run, anomalies):
    """Alert on known function codes whose rate rose 2+ std dev over baseline."""
    if anomalies.empty:
        return
    for ips_and_code in anomalies.columns:
        if run.skip(ips_and_code):
            continue
        ips, protocol, code = ips_and_code.split(":")
        timestamp = _first_active_time(anomalies, ips_and_code)
        alert = _ot_alert(
            run,
            "increased function code rate",
            f"Increased rate of {protocol}:{code} on {ips} starting at {timestamp}",
            ips,
            protocol,
            "function_code",
            code,
            timestamp,
        )
        run.emit(alert, [ips_and_code])


@register_analytic(
    key="OTFunctionCodeAnomalyAnalytic",
    family="ot_network",
    replace=True,
    data_source=ElasticsearchSource(),
    training=Supervised(),
    persistence=JoblibArtifact(),
    fit_idx={"index_name": "arkime_sessions3-*"},
    run_idx={"index_name": "arkime_sessions3-*"},
    out_idx={"index_name": "parseus_ot_function_code-out"},
)
class OTFunctionCodeAnomalyAnalytic(Analytic):
    """
    Name: OTFunctionCodeAnomalyAnalytic

    How: Analytic that Calculates OT (DNP3, Modbus) function codes per source/dest ip based on their rate per {time_interval}. If the rate increases or new function codes show up in run time different from fit time, an alert will fire. Combines events to fire alerts for suspected specific OT attack classes.

    When: Detect device that might be compromised and or being enumerated to see what type of functionality is on the device

    Usage: Not Listed

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

    Tags:
        + Change
    """

    def __init__(self, verbose=0, pull_duration=300, **kwargs):
        super().__init__(pull_duration=pull_duration, **kwargs)

        self.logic_ = OutsideStdDevModel()

        # NOTE: Malcolm does not log SEL FM or MMS.
        protocol_agg = {
            "dnp3": {
                "terms": {"field": "zeek.dnp3_objects.function_code"},
            },
            "modbus": {
                "terms": {"field": "zeek.modbus.func"},
            },
        }

        # Needed for calculations
        self.hours = 1
        time_intervals = f"{self.hours}h"
        composite_agg = {
            "composite": {
                "composite": {
                    "size": 10000,
                    "sources": [
                        {
                            "date": {
                                "date_histogram": {
                                    "field": "@timestamp",
                                    "fixed_interval": time_intervals,
                                }
                            }
                        }
                    ],
                },
                "aggs": {
                    "source_ips": {
                        "terms": {"field": "source.ip"},
                        "aggs": {
                            "destination_ips": {
                                "terms": {"field": "destination.ip"},
                                "aggs": protocol_agg,
                            }
                        },
                    }
                },
            }
        }

        query = {
            "bool": {
                "must": [
                    {"term": {"event.provider": "zeek"}},
                    {
                        "bool": {
                            "should": [
                                {"term": {"event.dataset": "dnp3"}},
                                {"term": {"event.dataset": "modbus"}},
                            ],
                        }
                    },
                ]
            }
        }

        self.protocols = [protocol for protocol in protocol_agg.keys()]

        # The transform function that will be called by the framework
        ot_function_code_transform = partial(
            function_code_transform, protocols=self.protocols
        )
        self._transform = ot_function_code_transform

        self._query = query
        self._composite_agg = composite_agg
        self.predict_results_doc = (
            "Baseline and alerts from new/increased OT functions."
        )
        self.fit_results_doc = """
        Baseline training information containing the top 10 connections
        with the lagest sum of bytes in the training set.
        """

    def data_source(self) -> ElasticsearchSource:
        """Return the OT function-code query and hourly composite aggregation."""
        index_query = IndexQuery(
            query=self._query, composite_agg=self._composite_agg
        )
        return ElasticsearchSource(fit=index_query, run=index_query)

    @classmethod
    def requires_fit(cls) -> bool:
        """
        True if the analytic must be fit before being used, or False otherwise.
        This may be set to false if the analytic is Fit and saved before hand.
        """
        return True

    def _fit(self, *func_codes):
        for func_code in func_codes:
            self._log.debug(f"{type(func_code)}, {len(func_codes)}")
            self.logic_.fit_table(func_code)

    def _run(self, start, end, func_codes_dataset, state, **other_docs):
        # Per-finding suppression: re-alerting for the same OT function-code finding is
        # controlled by the analytic's configured suppression window.
        run = _AlertRun(SuppressionWindow(state, self.suppression_), window_now(end), end)

        # Find new function codes, not seen in the fit baseline
        orig_columns = set(self.logic_.stats_df.index)
        new_func_codes = set(func_codes_dataset.columns) - orig_columns

        # Find anomalous high amounts of a function code
        _, anomaly_func_code_results = self.logic_.run_table(
            None, func_codes_dataset, buffer=3
        )

        _dos_alerts(run, func_codes_dataset, self.hours)
        _enumeration_alerts(run, func_codes_dataset, new_func_codes)
        self._log.debug(f"New func codes analysis {new_func_codes}")
        _new_code_alerts(run, func_codes_dataset, new_func_codes)
        self._log.debug("Calculating increased Function Rate alerts")
        _rate_alerts(run, anomaly_func_code_results)

        self.run_alerts = run.alerts
        return run.alerts

    def get_fit_results(self):
        """
        Formats baseline alerts as a pandas dataframe.
        """
        return pd.DataFrame(self.logic_.stats_df)
