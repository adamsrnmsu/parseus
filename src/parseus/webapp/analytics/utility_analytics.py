import json
import logging

import pandas as pd
from django.db import transaction

from parseus.analytics.capabilities import (
    ElasticsearchSource,
    IndexQuery,
    validate_source_config,
)
from parseus.analytics.spec import AnalyticSpec, load_class
from parseus.datastore.query_api import push
from parseus.interfaces.common import fit, run
from parseus.interfaces.config import get_analytic_config

from .models import (
    Analytic,
    AnalyticUserInput,
    TrainedModel,
    TrainedModelMetadata,
)
from .persistence import DbArtifactStore

logger = logging.getLogger("parseus")
ana_logger = logging.getLogger("analytics")


_INDEX_KEYS = {"fit_idx", "run_idx", "out_idx"}


def _merge_index_cfg(base: dict, override: dict) -> dict:
    """Merge index config dictionaries without dropping unspecified keys."""
    merged = dict(base or {})
    merged.update(override or {})
    return merged


def _merge_analytic_inputs(*configs: dict | None) -> dict:
    """Merge analytic config/user-input dictionaries.

    Later configs win. Index dictionaries are merged shallowly so that overriding
    an index name does not accidentally discard filters/tags populated from
    source_config.
    """
    merged = {}

    for cfg in configs:
        if not cfg:
            continue

        for key, value in cfg.items():
            if key in _INDEX_KEYS and isinstance(value, dict):
                merged[key] = _merge_index_cfg(merged.get(key, {}), value)
            elif key == "init_args" and isinstance(value, dict):
                merged[key] = {
                    **dict(merged.get(key, {}) or {}),
                    **value,
                }
            else:
                merged[key] = value

    return merged


def _analytic_config_from_ui(analytic_obj, user_input=None):
    """Build resolved runtime config from spec defaults, DB/UI config, and input."""
    ui_input = analytic_obj.ui.get_analytic_cfg() if analytic_obj.ui else {}
    merged_input = _merge_analytic_inputs(ui_input, user_input or {})

    return get_analytic_config(
        user_input=merged_input,
        analytic_name=analytic_obj.name,
    )


def _fit_index_query(analytic) -> IndexQuery:
    """Return the analytic's declared fit-phase IndexQuery, or an empty one.

    Used to record query metadata for a trained model; tolerant of analytics
    that declare no Elasticsearch source.
    """
    data_source = analytic.data_source() if hasattr(analytic, "data_source") else None
    if isinstance(data_source, ElasticsearchSource) and isinstance(
        data_source.fit, IndexQuery
    ):
        return data_source.fit
    return IndexQuery()


def _build_source_config(spec) -> dict:
    """Build a spec's ``source_config`` from its data source's declared schema.

    Reads the fields the source advertises (``config_schema``) and fills each
    from the spec's deployment values (index filters, schedule, pull duration),
    falling back to the field's declared default. Generic across source types:
    a source with no declared config yields an empty ``source_config``.
    """
    data_source = spec.data_source
    if data_source is None:
        return {}
    # Candidate deployment values, keyed to the field names sources declare.
    candidates = {
        "fit_idx_filter": spec.fit_idx.get("filter", []),
        "run_idx_filter": spec.run_idx.get("filter", []),
        "schedule": spec.schedule,
        "pull_duration": spec.pull_duration,
    }
    source_config = {}
    for field in data_source.config_schema():
        if field.name in candidates:
            source_config[field.name] = candidates[field.name]
        else:
            source_config[field.name] = field.resolve_default()
    return validate_source_config(data_source, source_config)


def _get_sysmon_rules(class_name):
    """Return UI-managed Sysmon rules for an analytic class if alerts is installed.

    The alerts app is an *optional* dependency: it owns Sysmon rules. When it is
    not installed, analytics runs without UI-managed rules rather than failing.
    """
    from django.apps import apps as django_apps

    if not django_apps.is_installed("parseus.webapp.alerts"):
        return []
    try:
        from parseus.webapp.alerts.models import get_sysmon_rules_for_analytic_class
    except Exception:
        return []
    return get_sysmon_rules_for_analytic_class(class_name) or []


def with_operator_rules(analytic_class, init_args):
    """Return ``init_args`` with operator-managed rules merged in.

    Fetches the operator's rules (currently the UI-managed Sysmon rules, honouring
    the optional ``alerts`` app) and delegates the actual merge to the analytic
    via :meth:`OperatorRuleMixin.merge_rules`, so the per-analytic rule binding
    lives on the analytic, not in a class-name map here. Analytics that do not
    use operator rules are returned unchanged.
    """
    class_name = getattr(analytic_class, "__name__", str(analytic_class)).split(".")[-1]
    rules = _get_sysmon_rules(class_name)
    merge = getattr(analytic_class, "merge_rules", None)
    if not rules or merge is None:
        return dict(init_args or {})
    return merge(init_args, rules)


def unsupervised_predict(analytic_obj, user_input, logger):
    analytic = load_class(analytic_obj.analytic_class)
    analytic_config = _analytic_config_from_ui(
        analytic_obj,
        user_input=user_input,
    )
    init_args = with_operator_rules(
        analytic,
        analytic_obj.ui.init_args,
    )
    init_args.setdefault("suppression", analytic_config.suppression)
    loaded_analytic = analytic(log=ana_logger, **init_args)

    # Load run-to-run state from the analytic's UI row (unsupervised analytics
    # have no TrainedModel to persist onto), so suppression memory survives
    # across predict runs.
    ui_row = analytic_obj.ui
    state_before = ui_row.run_state or {}
    loaded_analytic.import_state(state_before)

    alerts = run(
        ds_client=user_input["ds_host_url"],
        analytic=loaded_analytic,
        analytic_config=analytic_config,
    )
    logger.debug(alerts)

    # Persist evolved state back to the UI row, only when it changed.
    state_after = loaded_analytic.export_state()
    if state_after != state_before:
        ui_row.run_state = state_after
        ui_row.save(update_fields=["run_state"])

    return alerts


def train(analytic_db_obj, user_input, logger):
    with transaction.atomic():
        analytic_class = load_class(analytic_db_obj.analytic_class)
        model_name = f"{analytic_db_obj.model}_{analytic_db_obj.name}"
        if user_input["user_save"]:
            instance_name = f"{model_name}_{user_input['user_save']}"
        else:
            instance_name = model_name
        if not analytic_class.requires_fit():
            logger.debug(f"{analytic_db_obj.name}: analytic didn't require fit")
            return

        init_args = with_operator_rules(
            analytic_class,
            analytic_db_obj.ui.init_args,
        )
        analytic_instance = analytic_class(log=ana_logger, **init_args)
        analytic_config = _analytic_config_from_ui(
            analytic_db_obj,
            user_input=user_input,
        )
        analytic_instance = fit(
            ds_client=user_input["ds_host_url"],
            analytic=analytic_instance,
            analytic_config=analytic_config,
        )
        if analytic_instance:
            if analytic_instance.requires_fit() and hasattr(
                analytic_instance, "get_fit_results"
            ):
                drawing_data = analytic_instance.get_fit_results()
            else:
                drawing_data = pd.DataFrame()
            if isinstance(drawing_data, pd.DataFrame):
                drawing_data = drawing_data.to_html(
                    classes="table table-striped table-bordered"
                )

            with transaction.atomic():
                artifact_fields = DbArtifactStore().build_fields(
                    analytic_instance.state_dict(),
                    analytic_class=analytic_instance.class_name,
                )

                m = TrainedModel(
                    name=model_name,
                    instance_name=instance_name,
                    family=analytic_db_obj.family,
                    artifact=artifact_fields["artifact"],
                    manifest=artifact_fields["manifest"],
                    analytic=analytic_db_obj,
                    drawing_data=drawing_data,
                    analytic_class=analytic_db_obj.analytic_class,
                )
                m.save()
                meta = TrainedModelMetadata(
                    model=m,
                    name=m.name,
                    instance_name=m.instance_name,
                    family=analytic_db_obj.family,
                    supervised=analytic_db_obj.requires_fit,
                    es_host=user_input["ds_host_url"].url,
                    idx=analytic_config.fit_idx,
                    query=_fit_index_query(analytic_instance).query,
                    comp_aggs=_fit_index_query(analytic_instance).composite_agg,
                    return_fields=_fit_index_query(analytic_instance).return_fields
                    or [],
                    ip_filter=user_input.get("ip_filter"),
                    start_date=user_input.get("fit_start"),
                    end_date=user_input.get("fit_end"),
                    init_args=init_args,
                    fit_idx={"fit_idx": analytic_db_obj.ui.fit_idx_name},
                    run_idx={"run_idx": analytic_db_obj.ui.run_idx_name},
                )
                meta.save()
            return
        logger.debug("Done Training")
        return


def predict(trained_model, user_input, logger):
    with transaction.atomic():
        analytic_obj = trained_model.analytic
        try:
            trained_analytic_meta = TrainedModelMetadata.objects.get(
                model=trained_model
            )
            logger.debug(f"predicting model {trained_model.name}")
        except Exception as e:
            raise ValueError(f"Issue finding relevant Trained Model Info: {e}")
        a_class = trained_model.analytic_class
        analytic_cls = load_class(a_class)
        analytic_config = _analytic_config_from_ui(
            analytic_obj,
            user_input=user_input,
        )
        init_args = with_operator_rules(
            analytic_cls,
            trained_analytic_meta.init_args,
        )
        init_args.setdefault("suppression", analytic_config.suppression)
        analytic = analytic_cls(**init_args, log=ana_logger)
        analytic.load_state_dict(DbArtifactStore().load_from_instance(trained_model))
        if hasattr(analytic, "apply_rule_state"):
            analytic.apply_rule_state(init_args)

        # Load run-to-run state (suppression memory, dedup marks) from the row so
        # it survives across predict runs, separate from the immutable model blob.
        state_before = trained_model.run_state or {}
        analytic.import_state(state_before)

        alerts = run(
            ds_client=user_input["ds_host_url"],
            analytic=analytic,
            analytic_config=analytic_config,
        )
        logger.debug(alerts)

        # Persist evolved run-to-run state back to the row, only when it changed.
        state_after = analytic.export_state()
        if state_after != state_before:
            trained_model.run_state = state_after
            trained_model.save(update_fields=["run_state"])

        return {
            "outidx": analytic_obj.ui.out_idx_name,
            "alerts": alerts
        }


def push_results_to_es(data, es_host, out_idx, logger):
    try:
        d = json.loads(data)
        push(ds_client=es_host, index=out_idx, document_list=d)
        logger.info(f"{len(d)} results pushed")
        return
    except Exception as e:
        logger.error(e)
        return e


def upload_analytic_config():
    """Sync the DB analytic records with the descriptor registry.

    The registry, populated by ``@register_analytic`` decorators, is the source
    of truth for which analytics exist and their default deployment settings.

    This upserts each ``Analytic`` row by its natural key (``analytic_class``)
    instead of deleting and recreating every row. ``Analytic.id`` is a FK target
    for ``TrainedModel.analytic`` (``on_delete=SET_NULL``); replacing every row
    on each install/upgrade silently severs that link on every trained model.

    An ``Analytic`` row is never hard-deleted by this sync, even when its
    ``analytic_class`` disappears from the registry (e.g. an uninstalled
    plugin): it is soft-deleted instead, via ``is_active=False``, following
    the standard "reference row with live FK dependents" pattern (akin to
    ``ON DELETE RESTRICT``) — any existing ``TrainedModel``/``AnalyticUserInput``
    data stays intact and visible, just no longer offered for new runs. If the
    analytic reappears in the registry on a later sync, it is reactivated.
    Hard-deleting a retired analytic (and its trained models) is a deliberate,
    separate operator action, not a side effect of install/upgrade.
    """
    from parseus.analytics.registry import discover_analytics

    with transaction.atomic():
        analytic_specs = discover_analytics()
        seen_class_paths = {spec.class_path for spec in analytic_specs}

        for spec in analytic_specs:
            save_analytic(spec)

        retired = Analytic.objects.exclude(
            analytic_class__in=seen_class_paths
        ).filter(is_active=True).update(is_active=False)

        logger.info(f"Synced {len(analytic_specs)} analytics")
        if retired:
            logger.info(
                f"Retired {retired} analytics no longer provided by the registry"
            )

    logger.info("Finished populating analytics")


def save_analytic(spec: AnalyticSpec):
    """Upsert a registered analytic spec's DB-tracked state.

    Looks the row up by its natural key (``analytic_class``) so re-syncing
    (every install/upgrade) reactivates an existing row in place rather than
    deleting and recreating it. Preserving the row's PK keeps any
    ``TrainedModel.analytic`` FK pointing at it intact, and leaves its existing
    ``AnalyticUserInput`` (operator-set index names, ``init_args``,
    ``suppression``, ``source_config``) untouched. A new ``AnalyticUserInput``
    with spec defaults is only created the first time an analytic is seen.
    Nothing from the spec is copied onto the row itself — ``family``,
    ``requires_fit``, and all display metadata are resolved from this same
    spec at render time via ``Analytic.spec`` (see the model docstring), not
    duplicated into columns. Reappearing in the registry (e.g. a
    previously-retired plugin reinstalled) reactivates the row.
    """
    try:
        analytic_obj = Analytic.objects.filter(
            analytic_class=spec.class_path
        ).first()

        if analytic_obj is None:
            fit_idx = spec.fit_idx
            run_idx = spec.run_idx
            out_idx = spec.out_idx

            analytic_ui = AnalyticUserInput(
                init_args=spec.init_args,
                fit_idx_name=fit_idx.get("index_name", "packets*"),
                run_idx_name=run_idx.get("index_name", "packets*"),
                out_idx_name=out_idx.get("index_name", "parseus-out"),
                suppression=getattr(spec, "suppression", "0s") or "0s",
                source_config=_build_source_config(spec),
            )
            analytic_ui.save()

            analytic_obj = Analytic(analytic_class=spec.class_path, ui=analytic_ui)

        analytic_obj.is_active = True
        analytic_obj.save()

        logger.info(f"Added analytic: {spec.key}")
    except Exception as e:
        raise ValueError(f"Issue loading {spec.key} analytic information: {e}") from e

