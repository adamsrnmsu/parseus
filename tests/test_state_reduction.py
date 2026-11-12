"""Behavior of register-state clustering on pandas data.

pandas 3 (copy-on-write) hands out read-only arrays from DataFrame.values; hdbscan's
Cython code needs a writable buffer, which broke the Registers "State" tab with
"Cannot create writable memory view from read-only memoryview".
"""

import numpy as np
import pandas as pd

from parseus.preprocessors.state_reduction import (
    cluster_features,
    make_clustered_states_df,
)


def _two_level_reg_state():
    index = pd.date_range("2026-11-11", periods=60, freq="s")
    return pd.DataFrame(
        {
            "gate": np.r_[np.zeros(30), np.ones(30)],
            "valve": np.r_[np.ones(30), np.zeros(30)],
        },
        index=index,
    )


def test_cluster_features_separates_each_registers_levels():
    clustered = cluster_features(_two_level_reg_state())

    assert list(clustered.columns) == ["gate", "valve"]
    assert clustered.index.equals(_two_level_reg_state().index)
    for column in clustered.columns:
        labels = clustered[column]
        assert labels.iloc[:30].nunique() == 1
        assert labels.iloc[30:].nunique() == 1
        assert labels.iloc[0] != labels.iloc[-1]


def test_make_clustered_states_df_numbers_states_in_order_of_appearance():
    states = make_clustered_states_df(_two_level_reg_state(), cluster_value=0.06)

    assert states["state_id"].iloc[:30].eq(1).all()
    assert states["state_id"].iloc[30:].eq(2).all()
