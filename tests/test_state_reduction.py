"""Behavior of register-state clustering on pandas data.

pandas 3 (copy-on-write) hands out read-only arrays from DataFrame.values; hdbscan's
Cython code needs a writable buffer, which broke the Registers "State" tab with
"Cannot create writable memory view from read-only memoryview".

The tests that check how labels become state ids fake HDBSCAN (``fake_hdbscan``):
the real one is slow and stochastic.
"""

import numpy as np
import pandas as pd
import pytest

from parseus.preprocessors import state_reduction
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
        assert (labels != -1).all()
        assert labels.iloc[:30].nunique() == 1
        assert labels.iloc[30:].nunique() == 1
        assert labels.iloc[0] != labels.iloc[-1]


def test_make_clustered_states_df_numbers_states_in_order_of_appearance():
    states = make_clustered_states_df(_two_level_reg_state(), cluster_value=0.06)

    assert states["state_id"].iloc[:30].eq(1).all()
    assert states["state_id"].iloc[30:].eq(2).all()


def test_make_clustered_states_df_accepts_read_only_float_values():
    # One float column gives a C-contiguous read-only array that hdbscan won't copy.
    reg_state = _two_level_reg_state()[["gate"]]
    states = make_clustered_states_df(reg_state, cluster_value=0.06)

    assert states["state_id"].iloc[:30].eq(1).all()
    assert states["state_id"].iloc[30:].eq(2).all()


class FakeHDBSCAN:
    created = []
    labels = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakeHDBSCAN.created.append(self)

    def fit(self, values):
        self.fitted = values
        self.labels_ = list(FakeHDBSCAN.labels)


@pytest.fixture
def fake_hdbscan(monkeypatch):
    FakeHDBSCAN.created = []
    monkeypatch.setattr(state_reduction.hdbscan, "HDBSCAN", FakeHDBSCAN)


@pytest.mark.usefixtures("fake_hdbscan")
def test_state_ids_number_clusters_in_order_of_first_appearance():
    FakeHDBSCAN.labels = [7, -1, 3, 7, -1, 3, 9]
    binned = pd.DataFrame({"x": range(7)}, index=list("abcdefg"))
    result = state_reduction.make_clustered_states_df(binned, 0.1)
    assert result["ClusterID"].tolist() == [7, -1, 3, 7, -1, 3, 9]
    assert result["state_id"].tolist() == [1, -5, 2, 1, -5, 2, 3]
    assert result.index.tolist() == list("abcdefg")


@pytest.mark.usefixtures("fake_hdbscan")
def test_hamming_metric_unless_pca():
    FakeHDBSCAN.labels = [0]
    binned = pd.DataFrame({"x": [1]})
    state_reduction.make_clustered_states_df(binned, 0.25)
    state_reduction.make_clustered_states_df(binned, 0.25, pca=True)
    plain, pca = FakeHDBSCAN.created
    assert plain.kwargs == {"cluster_selection_epsilon": 0.25, "metric": "hamming"}
    assert pca.kwargs == {"cluster_selection_epsilon": 0.25}
