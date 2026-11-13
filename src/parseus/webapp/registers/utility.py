import json
import logging
import math
from itertools import chain

import numpy as np
import pandas as pd

# new package? is this necessary?
from more_itertools import windowed

from parseus.datastore.query_api import get_dnp3_points, get_modbus_registers
from parseus.preprocessors.reg_annotations import create_annotated_reg
from parseus.preprocessors.reg_ops import packet_to_regops
from parseus.preprocessors.reg_state import load_regstate, sample_weights
from parseus.preprocessors.reg_updates import (
    make_reg_updates_df,
    map_shortname_to_canonical,
)
from parseus.preprocessors.state_reduction import (
    cluster_features,
    make_clustered_states_df,
    reduce_dimensionality,
)
from parseus.preprocessors.transform import one_to_many
from parseus.preprocessors.util import build_stats_frame

logger = logging.getLogger("archi")


def get_reg_state(index=None, start=None, end=None, dshost=None):
    """
    Get register state data from the given Elastic index. Elastic settings are pulled
    from the first available Elastic_Setting object.

    Can raise several different errors that should be handled by the caller.
    """
    # Pull packets from elastic index
    # Modbus registers and DNP3 points (both written by the Zeek ingestor)
    packets = chain(
        get_modbus_registers(dshost, index, start=start, end=end),
        get_dnp3_points(dshost, index, start=start, end=end),
    )
    # Convert packets to register operations (dict with src, dst, etc.)
    reg_ops = one_to_many(packets, transformer=packet_to_regops)
    # Convert register operations to register state (Pandas DF)
    return load_regstate(reg_ops, logger)


def get_summary_from_reg_state(reg_state):
    """
    Get summary info (timeline, conversations count, unique timestamps) from the given
    reg_state data
    """
    # Get packet list start time
    start = reg_state.first_valid_index()
    # Get packet list end time
    end = reg_state.index[-1]
    # Create str with start and end times
    ts_format = "%B %d %Y %H:%M:%S.%f"
    start_end = f"{start.strftime(ts_format)[:-3]} - {end.strftime(ts_format)[:-3]}"
    # Create human-readable length
    logger.info(f"capture length {end - start}")
    capture_length = end - start
    # Create dict with packet list summary stats
    summary = {
        "timeline": {
            # total length of packet list
            "length": str(capture_length),
            # str with start and end times
            "start_end": start_end,
        },
        # number of conversations (each column = one conversation)
        "conversations": len(reg_state.columns),
        # number of unique timestamps
        "unique_timestamps": reg_state.shape[0],
    }
    return summary


def get_counts_from_reg_state(reg_state, include_points=False):
    """
    Get register type counts (total and str of individuals) form the given reg_state
    data
    """
    # Create annotations from register state
    annotations = create_annotated_reg(reg_state)
    # get register type counts (e.g. "Unchanged": 1)
    reg_counts = annotations["Reg_Type"].value_counts().to_dict()
    # count all registers
    reg_counts_total = sum(reg_counts.values())
    # get constant registers count
    constant_regs = reg_counts.get("Unchanging", 0)
    # convert binary, analog, and digital to a simple "variable" type count
    variable_regs = reg_counts.get("Binary", 0)
    variable_regs += reg_counts.get("Digital", 0)
    variable_regs += reg_counts.get("Analog", 0)
    # create a string listing the values
    reg_counts_list = f"{constant_regs} constant, {variable_regs} variable"
    counts = {
        "total": reg_counts_total,
        "list": reg_counts_list,
    }
    return counts


def get_conversations_from_reg_state(reg_state, include_points=False):
    """
    Get list of conversations from the given reg_state data in the format:

    {
        "IP1->IP2": {
            "REG1": stats (min, max, mean, etc.),
            ...
        },
        ...
    }

    In addition to stats, setting include_points to True will add a list of all points
    to each register for graphing purposes.
    """
    # setup the short names for the x axis
    short_to_con = map_shortname_to_canonical(reg_state)
    con_to_short = {y: x for x, y in short_to_con.items()}
    # Create annotations from register state
    annotations = create_annotated_reg(reg_state)
    # generate stats for each conversation
    stats_frame = build_stats_frame(reg_state, weights=sample_weights(reg_state))
    # replace NaN with None for JSON serialization
    stats_frame = stats_frame.replace({np.nan: None})
    # lowercase column names (convenient to do here instead of on client side)
    stats_frame = stats_frame.rename(mapper=lambda n: n.lower())
    # separate stats columns by conversation and add points for each register
    conversations = {}
    for conversation, stats in stats_frame.items():
        # Split src->dst:register into src->dst and register
        src_dst, register = conversation.split(":")
        # Add src_dst pair to conversations if it does not already exist
        if src_dst not in conversations:
            conversations[src_dst] = {}
        # Add stats for this register
        conversations[src_dst][register] = stats.to_dict()
        if include_points:
            # Add data points for this register (for graphing)
            points = list(reg_state[[conversation]].itertuples(name=None))
            conversations[src_dst][register]["points"] = points
        # Add filter properties
        is_constant = annotations["Reg_Type"][conversation] == "Unchanging"
        conversations[src_dst][register]["constant"] = is_constant
        # add short name for legend
        conversations[src_dst][register]["short_name"] = con_to_short[conversation]
    return conversations


def get_clustered_states_df(reg_state):
    if reg_state.empty:
        return pd.DataFrame({"ClusterID": [], "state_id": []})
    pca_cluster_value = 0.02
    pca_df = reduce_dimensionality(reg_state)
    binned_pca = cluster_features(pca_df, pca_cluster_value)
    return make_clustered_states_df(binned_pca, cluster_value=0.05, pca=True)


def get_clustered_states_dict(reg_state):
    if reg_state.empty:
        return {}
    clustered_states = get_clustered_states_df(reg_state).to_dict()
    # Convert pandas Timestamps to strings so they can be serialized
    ts_format = "%Y-%m-%dT%H:%M:%S.%f"
    clustered_states["ClusterID"] = {
        k.strftime(ts_format)[:-3]: v for k, v in clustered_states["ClusterID"].items()
    }
    clustered_states["state_id"] = [
        [k.strftime(ts_format)[:-3], v] for k, v in clustered_states["state_id"].items()
    ]
    return clustered_states


def get_reg_updates_heatmap_df(reg_state):
    if reg_state.empty:
        return pd.Dataframe()
    reg_updates_df = make_reg_updates_df(reg_state)
    resample_rate = calculate_resample(
        reg_updates_df.index.max(), reg_updates_df.index.min()
    )
    return reg_updates_df.resample(resample_rate).sum()


# helper function to calculate the resample rate dynamically for the heatmap graph
def calculate_resample(max, min):
    resample_rate = (max - min).total_seconds()
    quantifier = "s"

    if resample_rate > 60:
        # minutes
        resample_rate = resample_rate / 60
        quantifier = "min"
        if resample_rate > 60:
            # hours
            resample_rate = resample_rate / 60
            quantifier = "h"
            if resample_rate > 24:
                # days
                resample_rate = resample_rate / 24
                quantifier = "d"
                if resample_rate > 365:
                    # years
                    resample_rate = resample_rate / 365
                    quantifier = "YE"

    # Max 10 groups
    resample_rate = math.ceil(resample_rate / 10)
    rate = f"{resample_rate}{quantifier}"
    return rate


def get_reg_updates_heatmap_dict(reg_state, og_reg_state):
    if reg_state.empty:
        return {}
    reg_updates = get_reg_updates_heatmap_df(reg_state).to_dict()
    logger.debug(reg_updates)

    # setup the short names for the x axis
    short_to_con = map_shortname_to_canonical(og_reg_state)
    con_to_short = {y: x for x, y in short_to_con.items()}

    return {
        con_to_short[k]: [[x, y] for x, y in v.items()] for k, v in reg_updates.items()
    }


def get_node_edge_df(reg_state):
    if reg_state.empty:
        return pd.Dataframe()
    df = get_clustered_states_df(reg_state)
    return df


def get_node_edge_dict(reg_state, register_name):
    if reg_state.empty:
        return {}
    df = get_node_edge_df(reg_state)
    value_list = df[register_name].to_list()
    edge_tuples = set(windowed(value_list, n=2, step=1))

    # create nodes and links list
    d = {
        "nodes": [{"id": node} for node in set(value_list)],
        "links": [{"source": x, "target": y} for x, y in edge_tuples],
    }
    logger.debug(json.dumps(d))

    return json.dumps(d)
