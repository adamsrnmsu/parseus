"""
Functions to generate states and analyze its frequency
"""

import logging
import time

import hdbscan
import numpy as np
import pandas as pd
from sklearn import preprocessing
from sklearn.decomposition import PCA

from parseus.util.numerical import normalize

LOGGER = logging.getLogger("parseus")


def reduce_dimensionality(reg_state):
    number_registers = reg_state.shape[1]
    reg_index = pd.DataFrame(index=reg_state.index)
    register_values = reg_state.reset_index(drop=True)
    scaled_reg_state = preprocessing.scale(register_values)
    pca = PCA(0.95)
    pca.fit(scaled_reg_state)
    pca_data = pca.transform(scaled_reg_state)
    pca_df = pd.DataFrame(
        data=pca_data,
        columns=[f"PCA_{i+1}" for i in range(pca.n_components_)],
        index=reg_index.index,
    )
    per_var = np.round(pca.explained_variance_ratio_ * 100, decimals=1)
    LOGGER.debug("PCA preview:\n%s", pca_df.head())
    LOGGER.info(
        "Dimensionality reduced from %s to %s", number_registers, pca.n_components_
    )
    LOGGER.debug("PCA variance ratio: %s", per_var)
    return pca_df


def _fit(clusterer, values):
    """Fit hdbscan on a writable float copy: pandas 3 (copy-on-write) returns
    read-only arrays from DataFrame.values, and hdbscan's Cython code cannot
    take a read-only buffer."""
    return clusterer.fit(np.array(values, dtype=float))


def cluster_features(reg_state, cluster_value=0.06):
    """
    Clusters each register by using hdbscan
    """
    start_time = time.time()
    reg_state = reg_state.bfill()
    item_value_dict = {}
    for label in reg_state.columns:
        normalized = normalize(reg_state.loc[:, [label]])
        clusterer = hdbscan.HDBSCAN(cluster_selection_epsilon=cluster_value)
        _fit(clusterer, normalized.values.reshape(-1, 1))
        item_value_dict[label] = clusterer.labels_
    new_df = pd.DataFrame(item_value_dict).set_index(reg_state.index)
    LOGGER.debug(
        "Clustered selected features in %.3f seconds",
        time.time() - start_time,
    )
    return new_df


def make_clustered_states_df(binned_reg_state, cluster_value, pca=False):
    start_time = time.time()
    if pca:
        LOGGER.debug("Using HDBSCAN Euclidean metric for PCA state clustering")
        clusterer = hdbscan.HDBSCAN(cluster_selection_epsilon=cluster_value)
    else:
        LOGGER.debug("Using HDBSCAN Hamming metric for register state clustering")
        clusterer = hdbscan.HDBSCAN(
            cluster_selection_epsilon=cluster_value, metric="hamming"
        )
    _fit(clusterer, binned_reg_state.values)
    previously_seen = {}
    state_id_list = []
    count = 0
    outliers = 0
    for i, label in enumerate(clusterer.labels_):
        if label == -1:
            outliers += 1
            state_id_list.append(-5)
            continue
        if not previously_seen.get(label):
            count += 1
            previously_seen[label] = count
        state_id_list.append(previously_seen.get(label))
    LOGGER.info("There were %s/%s outliers", outliers, len(clusterer.labels_))
    LOGGER.debug("Clustered state in %.3f seconds", time.time() - start_time)
    LOGGER.info("Total unique state_ids: %s", len(set(state_id_list)))
    states_df = pd.DataFrame(
        {"ClusterID": clusterer.labels_, "state_id": state_id_list},
        index=binned_reg_state.index,
    )
    return states_df
