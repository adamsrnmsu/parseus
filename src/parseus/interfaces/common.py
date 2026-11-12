"""Runtime interface for fitting and running analytics."""

import os
from datetime import timedelta

from parseus.datastore.client import DataStore
from parseus.datastore.sources.factory import build_data_source
from parseus.interfaces import LOGGER
from parseus.interfaces.config import get_analytic_config
from parseus.util.datetime_utils import datetime_parser


def _declared_data_source(analytic):
    """Return the analytic's data-source specification, or None.

    Delegates to ``analytic.data_source()`` so an analytic can build its spec
    from instance configuration.
    """
    data_source = getattr(analytic, "data_source", None)
    return data_source() if callable(data_source) else None


def fit(ds_client, analytic, analytic_config):
    """
    Fitting the analytic based on information in the configuration.
    Returns the fitted analytic instance.
    """
    LOGGER.info("Fitting: %s", analytic.__class__.__name__)
    data_source = build_data_source(
        ds_client,
        analytic_config,
        get_timerange=get_timerange,
        data_source=_declared_data_source(analytic),
    )
    analytic.fit(data_source.get_fit_data(analytic))
    return analytic


def run(ds_client, analytic, analytic_config):
    """
    Runs predictions for the analytic based on information in the configuration.
    Returns alerts produced.
    """
    LOGGER.info("Running: %s", analytic.__class__.__name__)
    data_source = build_data_source(
        ds_client,
        analytic_config,
        get_timerange=get_timerange,
        data_source=_declared_data_source(analytic),
    )
    tags = getattr(analytic_config, "tags", [])
    all_alerts = []

    for window in data_source.iter_run_windows(analytic):
        data = data_source.get_run_data(analytic, window)
        try:
            alerts = analytic.run_on_data(data, window.start, window.end)
            all_alerts.extend(alerts)
        except Exception:
            LOGGER.exception(
                "Exception when running between %s-%s for analytic: %s",
                window.start,
                window.end,
                analytic.__class__.__name__,
            )
            raise
        for alert in alerts:
            if tags:
                alert["tags"] = tags
        LOGGER.info(
            "Analytic %s alerted on %s events",
            analytic.__class__.__name__,
            len(alerts),
        )
    return all_alerts


def schedule_timestamp_range(analytic, user_input=None):
    analytic_config = get_analytic_config(
        user_input=user_input,
        analytic_name=analytic.__name__,
    )
    return get_timerange(get_ds_client(), analytic_config.run_idx)


def schedule_run_index(analytic, user_input=None) -> str:
    """Return the configured run index for a scheduled analytic."""
    analytic_config = get_analytic_config(
        user_input=user_input,
        analytic_name=analytic.__name__,
    )
    return analytic_config.run_idx


def source_has_data(ds_client, index_name) -> bool:
    """
    Return whether a source index exists and currently has documents.

    This lets scheduled callers distinguish a genuinely empty/missing source
    from a populated one, because ``get_min_timestamp``/``get_max_timestamp``
    fall back to epoch/now when the index is absent or empty.
    """
    try:
        if not ds_client.index_exists(index_name):
            return False
        return ds_client.get_document_count(index_name) > 0
    except Exception:
        LOGGER.exception(
            "Failed to determine document availability for index %s", index_name
        )
        return False



def get_ds_client() -> DataStore:
    datastore_host_url = os.environ.get("DATASTORE_HOST_URL", "http://localhost:9200")
    datastore_type = os.environ.get("DATASTORE_TYPE", "elasticsearch")
    datastore_auth = os.environ.get("DATASTORE_AUTH", None)
    return DataStore(datastore_host_url, basic_auth=datastore_auth, base=datastore_type)


def get_model_from_cache(models_dir, model_name, log, loaded_analytic):
    """Returns fitted analytic model if one already exists, otherwise returns None."""
    from parseus.analytics.persistence import FileArtifactStore

    store = FileArtifactStore(models_dir)
    if store.exists(model_name):
        log.info(
            f"Found existing {model_name} in {models_dir}. Using it instead of training."
        )
        loaded_analytic.load_state_dict(store.load(model_name))
        return loaded_analytic
    return None


def _none_if_blank(value):
    if isinstance(value, str) and not value.strip():
        return None
    return value


def get_timerange(ds_client, index_name, start=None, end=None):
    """
    Gets start and end times specified in the config file, or override
    with user provided values.
    """
    start = _none_if_blank(start)
    end = _none_if_blank(end)

    # add a minimal timedelta to be inclusive of the whole timerange.
    epsilon = timedelta(milliseconds=1)

    if start is None:
        start = ds_client.get_min_timestamp(index_name)
    start = datetime_parser(start)

    if end is None:
        end = ds_client.get_max_timestamp(index_name) + epsilon
    end = datetime_parser(end)

    # If the user did not give any change in the times, assume they want the
    # full timerange possible
    time_diff = end - start
    if time_diff.total_seconds() == 0:
        start = datetime_parser(ds_client.get_min_timestamp(index_name))
        end = datetime_parser(ds_client.get_max_timestamp(index_name)) + epsilon

    return start, end
