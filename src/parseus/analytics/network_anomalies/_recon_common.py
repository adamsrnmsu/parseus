"""Shared helpers for the network reconnaissance analytics."""

from datetime import datetime, timezone

import pandas as pd


def build_interval_counter(interval_data, outer_agg, inner_agg, **kwargs):
    """
    Takes documents returned from a nested datastore aggregation.
    Creates a dataframe with columns for the separate aggregation values.
    """
    data_tuples = {}
    for interval in interval_data:
        date_time = datetime.fromtimestamp(
            interval["key"]["timestamp"] / 1000, tz=timezone.utc
        )
        source_ip = interval["key"][outer_agg]
        counts = interval[inner_agg]["value"]
        if date_time not in data_tuples:
            data_tuples[date_time] = {}
        data_tuples[date_time][source_ip] = counts
    df = pd.DataFrame.from_dict(data_tuples, orient="index")
    return df
