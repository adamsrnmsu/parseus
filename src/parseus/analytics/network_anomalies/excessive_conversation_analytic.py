"""Analytic that spots excessive IP-to-IP conversation counts (reconnaissance)."""

from functools import partial

import pandas as pd

from parseus.analytics.base import (
    Analytic,
    OutsideStdDevModel,
    replace_with_thresholds_iqr,
)
from parseus.analytics.capabilities import (
    ElasticsearchSource,
    IndexQuery,
    JoblibArtifact,
    Supervised,
)
from parseus.analytics.core import (
    SuppressionWindow,
    build_ecs_alert,
    window_now,
)
from parseus.analytics.filters import private_ip_filter
from parseus.analytics.network_anomalies._recon_common import build_interval_counter
from parseus.analytics.queries import histogram_cardinality_agg
from parseus.analytics.registry import register_analytic


def _conversation_agg(calendar_interval: str = "minute") -> dict:
    """Composite agg: source IPs bucketed by time -> distinct destination IPs."""
    return histogram_cardinality_agg(
        term_field="source.ip",
        term_label="source_ips",
        metric_field="destination.ip",
        metric_label="destination_ips",
        calendar_interval=calendar_interval,
    )


@register_analytic(
    key="ExcessiveConversationAnalytic",
    family="network_anomalies",
    replace=True,
    data_source=ElasticsearchSource(
        fit=IndexQuery(composite_agg=_conversation_agg()),
        run=IndexQuery(composite_agg=_conversation_agg()),
    ),
    training=Supervised(),
    persistence=JoblibArtifact(),
    init_args={"calendar_interval": "minute"},
    fit_idx={
        "index_name": "packets-*",
        "filter": private_ip_filter("source.ip", "destination.ip"),
    },
    run_idx={
        "index_name": "packets-*",
        "filter": private_ip_filter("source.ip", "destination.ip"),
    },
    out_idx={"index_name": "parseus-excessive-conversation-out"},
)
class ExcessiveConversationAnalytic(Analytic):
    """
    Name: ExcessiveConversationAnalytic
    How: Counts the distinct destination IPs each source IP contacts per time
         bucket and alerts when that count deviates from the trained baseline.

    When: Detecting beaconing, scanning, or exfiltration over the network.

    Usage: Not Listed

    Mitre-Data-Sources:
        + Network Traffic: Network Traffic Content

    Mitre-Techniques:
        + Network Service Scanning (T0841)
        + Network Sniffing (T1040)
        + Automated Collection (T0802)
        + Theft of Operational Information (T0882)
        + collection (TA0100)
        + discovery (TA0102)

    Purdue-Levels:
        + level-1
        + level-2
        + level-3
        + level-4
        + level-5

    Tags:
        + beaconing
        + scanning
        + exfiltration
    """

    def __init__(self, pull_duration=90, calendar_interval="minute", **kwargs):
        """
        Sets up the analytic to receive packets, and store the
        source and destination IPs from those packets.
        """
        super().__init__(pull_duration=pull_duration, **kwargs)

        build_conv_counter = partial(
            build_interval_counter, outer_agg="source_ips", inner_agg="destination_ips"
        )
        self._transform = build_conv_counter
        self.convo_counts_ = pd.DataFrame()
        self.logic_ = OutsideStdDevModel()
        self.calendar_interval = calendar_interval
        self.predict_results_doc = """
        Shows any devices in a given hour/day where the amount of
        dest ips it attempts to reach fall outside the trained mean/stdev
        """
        self.fit_results_doc = """
        Shows a dataframe of unique dest ip counts  hourly/daily
        from every source ip seen in the given dataset.
        At this point, Data is not exact as Outliers have already
        been modified to to flatten the skew.
        """

    def data_source(self) -> ElasticsearchSource:
        """Build the ES source spec from the configured aggregation interval."""
        agg = _conversation_agg(self.calendar_interval)
        return ElasticsearchSource(
            fit=IndexQuery(composite_agg=agg),
            run=IndexQuery(composite_agg=agg),
        )

    def _fit(self, *regops_datasets):
        """
        Fits the analytic by storing IPs from conversations we have seen
        """
        self._log.info("Fitting using calendar_interval = %s", self.calendar_interval)
        self.convo_counts_ = pd.concat(regops_datasets)
        self.convo_counts_.fillna(0, inplace=True)
        # Replaced outliers skewing dataframe with IQR thresholds
        replace_with_thresholds_iqr(self.convo_counts_, self.convo_counts_.columns)
        self.logic_.fit_table(self.convo_counts_)

    def _run(self, start, end, transformed_documents, state, **other_docs):
        """
        return:
         Alerts for abnormal number of IP conversations
        """
        alerts = []
        suppression = SuppressionWindow(state, self.suppression_)
        now = window_now(end)

        predict_df = transformed_documents
        predict_df.fillna(0, inplace=True)

        _, results = self.logic_.run_table(None, predict_df, buffer=5)

        for ip in results:
            ip_alert = results[results[ip].notnull()][ip].to_dict()
            mean = self.logic_.stats_df.loc[ip, "mean"]
            std = self.logic_.stats_df.loc[ip, "std"]

            for time, count in ip_alert.items():
                if not suppression.should_emit(
                    "excessive_conversation",
                    ip,
                    now=now,
                ):
                    continue

                alert = build_ecs_alert(
                    "excess device conversations",
                    (
                        f"Excess Device Conversations: {ip} "
                        + f"usually talks to {round(mean)} "
                        + f"+/- {round(std)} other IP's, but "
                        + f"at {time}, it talked to {count} different IP's."
                    ),
                    1.0,
                    category=["network"],
                    timestamp=time,
                    event_extra={"timestamp": start},
                    source={"ip": ip},
                )
                alerts.append(alert)
        return alerts

    def get_fit_results(self):
        "returns conversation counts dataframe"
        return self.convo_counts_
