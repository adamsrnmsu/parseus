import json
import logging

from django.db import transaction
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseNotFound,
    HttpResponseServerError,
    JsonResponse,
)
from django.shortcuts import render
from django.template import loader
from django.views.decorators.http import require_http_methods

from parseus.webapp.project.views import _get_database_client, get_database_status
from parseus.webapp.registry import get_data_source_provider

from .models import Analytic, TrainedModel
from .utility_analytics import predict, push_results_to_es, train, unsupervised_predict

logger = logging.getLogger("parseus")

# Engine data-source type for PEAT filesystem output. The provider that serves
# it is registered by the pei app when installed; analytics only depends on the
# generic provider registry, not on pei directly.
PEAT_SOURCE_TYPE = "PeatFileData"


class DataSourceUnavailableError(Exception):
    """Raised when a required data-source provider is not installed."""


def analytics_index(request):
    template = loader.get_template("analytics/analytic_base.html")
    logger.info("Showing analytics home page")
    return HttpResponse(template.render(request=request))


# analytic_types value -> (requires_fit, is_peat, template); first match wins.
_ANALYTIC_LISTINGS = {
    "NetworkSupervised": (True, False, "analytics/supervised_analytics.html"),
    "NetworkUnsupervised": (False, False, "analytics/unsupervised_analytics.html"),
    "PeatSupervised": (True, True, "analytics/supervised_analytics.html"),
    "PeatUnsupervised": (False, True, "analytics/unsupervised_analytics.html"),
}


def _select_analytics(active_analytics, analytic_types_list):
    """Return ``(analytics, template_name)`` for the requested analytic types.

    :raises Http404: When none of the types is one of the four listed kinds.
    """
    for kind, (requires_fit, is_peat, template_name) in _ANALYTIC_LISTINGS.items():
        if kind in analytic_types_list:
            return [
                a
                for a in active_analytics
                if bool(a.requires_fit) == requires_fit
                and (a.family == "ot_internals") == is_peat
            ], template_name
    raise Http404(f"Unknown analytic type: {','.join(analytic_types_list)}")


def analytics(request, analytic_types):
    active_analytics = list(Analytic.objects.filter(is_active=True))
    analytics_qs, template_name = _select_analytics(
        active_analytics, analytic_types.split(",")
    )
    template = loader.get_template(template_name)
    families = [{"family": fam} for fam in sorted({a.family for a in analytics_qs})]
    # name/family are resolved from the registry spec, not DB columns, so
    # sorting happens in Python rather than via queryset.order_by(...).
    analytics = sorted(analytics_qs, key=lambda a: a.name)
    context = {
        "analytics": analytics,
        "families": families,
        "data": [],
        "analytic_type": analytic_types,
        "peat_available": get_data_source_provider(PEAT_SOURCE_TYPE) is not None,
    }
    return HttpResponse(template.render(context, request))


def _resolve_peat_index(index_name):
    """Resolve a PEAT dataset id to its engine path via the registered provider.

    The provider is registered by the pei app. If pei is not installed, no
    provider exists and PEAT-sourced analytics cannot run.
    """
    provider = get_data_source_provider(PEAT_SOURCE_TYPE)
    if provider is None:
        raise DataSourceUnavailableError(
            "PEAT data source is not available. Install the PEAT app to run "
            "PEAT-based analytics."
        )
    return provider.resolve(index_name)


def _get_analytic_input_from_request(request, fit_or_run):
    client = _get_database_client(request)
    user_input = {
        "user_save": request.POST.get("user_save", ""),
        "ip_filter": request.POST.get("ip_filter", ""),
        f"{fit_or_run}_start": request.POST.get("start_date"),
        f"{fit_or_run}_end": request.POST.get("end_date"),
        "ds_host_url": client,
    }
    index_name = request.POST.get("index_name")
    if index_name:
        user_input[f"{fit_or_run}_idx"] = {
            "index_name": _resolve_peat_index(index_name)
        }
    logger.debug(request.POST)
    return user_input


@require_http_methods(["POST"])
def train_model(request):
    analytic_pks = request.POST.getlist("analytic_pks[]", [])
    try:
        user_input = _get_analytic_input_from_request(request, "fit")
    except DataSourceUnavailableError as e:
        return HttpResponseBadRequest(str(e))

    results = []
    for pk in analytic_pks:
        obj = Analytic.objects.get(pk=pk, is_active=True)
        try:
            train(obj, user_input, logger)
            results.append({"status": "success", "model": obj.name})
        except Exception as e:
            logger.exception("Train did not work for analytic %s", obj.name)
            results.append({"status": "failed", "model": obj.name, "error": str(e)})
    return JsonResponse(results, safe=False)


@require_http_methods(["POST"])
def predict_peat_unsupervised(request):
    return predict_unsupervised(request)


@require_http_methods(["POST"])
def predict_network_unsupervised(request):
    http_response = get_database_status(request)
    json_response = json.loads(http_response.content)
    if not json_response["status"]:
        return HttpResponseBadRequest(json_response["message"])
    return predict_unsupervised(request)


def predict_unsupervised(request):
    analytic_pks = request.POST.getlist("analytic_pks[]", [])
    try:
        user_input = _get_analytic_input_from_request(request, "run")
    except DataSourceUnavailableError as e:
        return HttpResponseBadRequest(str(e))
    ana_results = {}
    for pk in analytic_pks:
        with transaction.atomic():
            obj = Analytic.objects.get(pk=pk, is_active=True)
            try:
                alerts = unsupervised_predict(obj, user_input, logger)
                ana_results[obj.name] = alerts
            except Exception:
                logger.exception("Predict did not work for analytic %s", obj.name)
                return HttpResponseBadRequest(
                    f"Prediction did not work for analytic {obj.name}"
                )
    context = {"analytics": ana_results}
    return render(request, "analytics/analytic_results.html", context)


def get_trained_models(request, model_types="All"):
    model_types = model_types.split(",")
    template = loader.get_template("analytics/trained_models.html")
    if "NetworkSupervised" in model_types:
        models = TrainedModel.objects.exclude(family="ot_internals").order_by(
            "-trainedmodelmetadata__created_at"
        )
    else:
        models = TrainedModel.objects.all().order_by(
            "-trainedmodelmetadata__created_at"
        )
    context = {"models": models}
    return HttpResponse(template.render(context, request))


def delete_trained_model(request, pk):
    try:
        model = TrainedModel.objects.get(pk=pk)
        model.delete()
    except Exception as e:
        return HttpResponseServerError(e)
    return HttpResponse(status=200)


@require_http_methods(["POST"])
def predict_model(request):
    model_pk = request.POST.get("model_pk", "")
    try:
        user_input = _get_analytic_input_from_request(request, "run")
    except DataSourceUnavailableError as e:
        return HttpResponseBadRequest(str(e))
    obj = TrainedModel.objects.get(pk=model_pk)
    try:
        data = predict(obj, user_input, logger)
        if data["alerts"] == []:
            data["alerts"] = "No results"
    except Exception as e:
        logger.exception("Predict did not work for trained model %s", obj.pk)
        return HttpResponseBadRequest(e)
    return JsonResponse(data, safe=False)


@require_http_methods(["POST"])
def push_results(request):
    outidx = request.POST.get("outidx", "")
    data = request.POST.get("es_data", "")

    if not outidx:
        return HttpResponseBadRequest("index was empty")
    if not data or data == '"No results"':
        return HttpResponseBadRequest("data was empty")

    ds_host = _get_database_client(request)
    if not ds_host.ping():
        return HttpResponseBadRequest(
            f"Elasticsearch host '{ds_host.url}' could not be reached"
        )

    err = push_results_to_es(data, ds_host, outidx, logger)
    if err:
        return HttpResponseBadRequest(err)
    return HttpResponse(status=200)


@require_http_methods(["GET"])
def peat_datasets(request):
    """Return selectable PEAT datasets from the registered provider.

    Delegates to whatever provider serves ``PeatFileData`` (registered by the
    pei app). Returns an empty list when no PEAT provider is installed, so the
    analytics UI degrades cleanly rather than calling into another app.
    """
    provider = get_data_source_provider(PEAT_SOURCE_TYPE)
    if provider is None:
        return JsonResponse([], safe=False)
    dataset_ids = [dataset["id"] for dataset in provider.get_datasets()]
    return JsonResponse(dataset_ids, safe=False)


@require_http_methods(["POST"])
def update_analytic_user_input(request, analytic_pk):
    logger.debug(request.POST)
    try:
        analytic = Analytic.objects.get(pk=analytic_pk)
    except Exception as e:
        return HttpResponseNotFound(e)
    try:
        analytic.ui.fit_idx_name = request.POST.get("fit_idx", analytic.ui.fit_idx_name)
        analytic.ui.run_idx_name = request.POST.get("run_idx", analytic.ui.run_idx_name)
        analytic.ui.out_idx_name = request.POST.get("out_idx", analytic.ui.out_idx_name)
        analytic.ui.init_args = request.POST.get("init_args", analytic.ui.init_args)
        analytic.ui.save()
        logger.info("analytic settings saved")
    except Exception as e:
        return HttpResponseServerError(e)

    return HttpResponse(status=200)
