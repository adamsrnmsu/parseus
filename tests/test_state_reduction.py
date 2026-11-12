"""Tests for state clustering helpers (HDBSCAN is faked: it is slow and stochastic)."""

import pandas as pd
import pytest

from parseus.preprocessors import state_reduction


class FakeHDBSCAN:
    created = []
    labels = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakeHDBSCAN.created.append(self)

    def fit(self, values):
        self.fitted = values
        self.labels_ = list(FakeHDBSCAN.labels)


@pytest.fixture(autouse=True)
def fake_hdbscan(monkeypatch):
    FakeHDBSCAN.created = []
    monkeypatch.setattr(state_reduction.hdbscan, "HDBSCAN", FakeHDBSCAN)


def test_state_ids_number_clusters_in_order_of_first_appearance():
    FakeHDBSCAN.labels = [7, -1, 3, 7, -1, 3, 9]
    binned = pd.DataFrame({"x": range(7)}, index=list("abcdefg"))
    result = state_reduction.make_clustered_states_df(binned, 0.1)
    assert result["ClusterID"].tolist() == [7, -1, 3, 7, -1, 3, 9]
    assert result["state_id"].tolist() == [1, -5, 2, 1, -5, 2, 3]
    assert result.index.tolist() == list("abcdefg")


def test_hamming_metric_unless_pca():
    FakeHDBSCAN.labels = [0]
    binned = pd.DataFrame({"x": [1]})
    state_reduction.make_clustered_states_df(binned, 0.25)
    state_reduction.make_clustered_states_df(binned, 0.25, pca=True)
    plain, pca = FakeHDBSCAN.created
    assert plain.kwargs == {"cluster_selection_epsilon": 0.25, "metric": "hamming"}
    assert pca.kwargs == {"cluster_selection_epsilon": 0.25}
