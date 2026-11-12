"""
This module contains analytics that operate on extended statistics information on
network data.
"""

import pandas as pd

from parseus.analytics.base import Analytic
from parseus.analytics.core import (
    SuppressionWindow,
    build_ecs_alert,
    window_now,
)
from parseus.analytics.capabilities import (
    ElasticsearchSource,
    IndexQuery,
    JoblibArtifact,
    Supervised,
)
from parseus.analytics.registry import register_analytic
from parseus.analytics.queries import CONN_DATASET_QUERY

_NEW_COMMUNICATION_COMPOSITE_AGG = {
    "aggs": {
        "protocol_convos": {
            "composite": {
                "size": 10000,
                "sources": [
                    {"source": {"terms": {"field": "source.ip"}}},
                    {"dest": {"terms": {"field": "destination.ip"}}},
                ],
            },
            "aggs": {"protocols": {"terms": {"field": "protocol"}}},
        }
    }
}


@register_analytic(
    key="NewCommunicationsAnalytic",
    family="network_anomalies",
    replace=True,
    data_source=ElasticsearchSource(
        fit=IndexQuery(
            query=CONN_DATASET_QUERY,
            composite_agg=_NEW_COMMUNICATION_COMPOSITE_AGG,
        ),
        run=IndexQuery(
            query=CONN_DATASET_QUERY,
            composite_agg=_NEW_COMMUNICATION_COMPOSITE_AGG,
        ),
    ),
    training=Supervised(),
    persistence=JoblibArtifact(),
    fit_idx={"index_name": "arkime_sessions3-*"},
    run_idx={"index_name": "arkime_sessions3-*"},
    out_idx={"index_name": "parseus_new_communications-out"},
)
class NewCommunicationsAnalytic(Analytic):
    """
    Name: NewCommunicationsAnalytic

    How: Reports new protocols, conversations, or IPs seen between every connected IP pair.

    When: Looking for new protocols or conversations, which is a weak indicator for lateral movement or enumeration

    Usage: Not Listed

    Mitre-Data-Sources:
        + Network Traffic: Network Traffic Content

    Mitre-Techniques:
        + User Execution (T0863)
        + Change Operating Mode (T0858)
        + collection (TA0100)
        + lateral-movement (TA0109)
        + initial-access (TA0108)
        + command-and-control (TA0101)

    Purdue-Levels:
        + level-1
        + level-2
        + level-3
        + level-4
        + level-5

    Tags:
        + New Protocol
    """

    def __init__(self, pull_duration=300, **kwargs):
        """
        Sets up the analytic to receive aggregation and store protocol conversations
        """
        super().__init__(pull_duration=pull_duration, **kwargs)

        self.predict_results_doc = """
        Alerts for run-data connections that are new relative to the training baseline,
        use protocols not seen for that source/destination pair during training, or
        contain IPs not observed during training. Repeated findings are controlled by
        the analytic suppression window.
        """
        self.fit_results_doc = """
        Baseline training information stored for each connection.
        Connection is defined as the (source, destination) tuple.
        A set of protocol stacks (each protocol name separated by
        colons) are stored for each connection.
        """

    def _fit(self, *regops_datasets):
        """
        Fits the analytic by storing the protocols seen for each source:dest IP pair

        Input: Raw aggregated training data from elasticsearch that is the result of an
               elasticsearch aggregation query in the user provided .yml file
        Function Side-effects: source,destination IPs stored as keys in dictionary with
               values being a set of protocols seen.
               After completion, state automatically saves the analytic (which contains the
               model) to the artifacts/models directory.
        """
        self.protocol_convos_ = {}
        self.seen_ips_ = set()

        for dataset in regops_datasets:
            for conversation in dataset:
                src = conversation["key"]["source"]
                dst = conversation["key"]["dest"]
                convo = (src, dst)

                proto_set = {
                    p["key"]
                    for p in conversation["protocols"]["buckets"]
                }

                self.protocol_convos_[convo] = proto_set
                self.seen_ips_.add(src)
                self.seen_ips_.add(dst)

        if not self.protocol_convos_:
            self._log.warning("Training Set Was Empty")
        else:
            self._log.info(f"Training Set contained {len(self.protocol_convos_)}")

    def _run(self, start, end, transformed_documents, state, **other_docs):
        """
        Description: Find any source:dest IP pairs that communicate protocols not seen
            in the training data (including new IP pairs and new IPs)
            Called by Analytic.run_on_data() once per run window.

        Inputs: start: The start timestamp of this run window
                       (now - pull_duration)
                end: The end timestamp of this run window (used as the
                   suppression clock)
                transformed_documents: The composite-aggregation buckets for this
                   window, produced by the query declared on the analytic's
                   @register_analytic data_source (see this module's
                   _NEW_COMMUNICATION_COMPOSITE_AGG)
                **other_docs: an unpacked dictionary containing additional docs. Typically ignored.
        Function side-effects: None
        Returns: An iterable of alert dicts for new conversations, new
            protocols, and new IPs
        """
        alerts = []
        suppression = SuppressionWindow(state, self.suppression_)
        now = window_now(end)

        for conversation in transformed_documents:
            src = conversation["key"]["source"]
            dst = conversation["key"]["dest"]
            protocol_set = {
                protocol["key"] for protocol in conversation["protocols"]["buckets"]
            }
            alerts.extend(
                self._conversation_alerts(
                    suppression, now, start, src, dst, protocol_set
                )
            )
            # Alert for any new IPs
            for ip in (src, dst):
                if ip not in self.seen_ips_ and suppression.should_emit(
                    "new_ip", ip, now=now
                ):
                    alerts.append(
                        build_ecs_alert(
                            "new ip alert",
                            f"New IP Alert: {ip}",
                            10.0,
                            category=["network"],
                            timestamp=start,
                            source={"ip": src},
                            destination={"ip": dst},
                        )
                    )

        return alerts

    def _conversation_alerts(self, suppression, now, start, src, dst, protocol_set):
        """Alerts for a new conversation, or for new protocols on a known one."""
        convo = (src, dst)
        if convo not in self.protocol_convos_:
            if not suppression.should_emit("new_conversation", src, dst, now=now):
                return []
            action, label, protocols = (
                "new conversation alert",
                "New Conversation Alert",
                sorted(protocol_set),
            )
        else:
            protocols = [
                protocol
                for protocol in sorted(protocol_set - self.protocol_convos_[convo])
                if suppression.should_emit("new_protocol", src, dst, protocol, now=now)
            ]
            if not protocols:
                return []
            action, label = "new protocol seen", "New Protocol Seen"
        return [
            build_ecs_alert(
                action,
                f"{label}: {src}->{dst}:{protocols}",
                10.0,
                timestamp=start,
                source={"ip": src},
                destination={"ip": dst},
                network={"new_protocols": protocols},
            )
        ]

    def get_fit_results(self):
        """
        Returns Dataframe containing baseline training information stored
        for each connection. Connection is defined as the (source, destination)
        tuple. A set of protocol stacks (each protocol name separated by
        colons) are stored for each connection.
        """
        results = {
            "ip_pair": [],
            "protocols": [],
        }
        for ip_pair, protocols in self.protocol_convos_.items():
            results["ip_pair"].append(ip_pair)
            results["protocols"].append(sorted(protocols))
        return pd.DataFrame(results)
