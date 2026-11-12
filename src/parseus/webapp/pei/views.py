import io
import json
import logging
import mimetypes
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlencode

import yaml
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView, View
from peat import generate_simple_config, module_api

from parseus.webapp.project.views import get_database_status, get_database_url

from .forms import PeatBuilderForm, PeatConfigForm, PeatDeviceForm
from .models import PeatConfig, PeatDevice, PeatPull, PeatScan
from .peat_locks import (
    PeatLockError,
    acquire_target_locks,
    build_owner_token_for_run,
    release_owner_locks,
    resolve_lock_targets,
)
from .tasks import peat_pull, peat_scan

LOGGER = logging.getLogger(__name__)


class PeatTargetSelectionError(ValueError):
    """Raised when selected configured hosts cannot form one PEAT invocation."""


def index(request):
    """Render the peat home page."""
    return render(request, "peat/home.html")


def get_peat_modules(request):
    return JsonResponse(module_api.names, safe=False)


def get_peat_module_defaults(request, module_name):
    """Return the default options that PEAT generates for a module."""
    matching_module = next(
        (
            peat_module
            for peat_module in module_api.names
            if peat_module.lower() == module_name.lower()
        ),
        None,
    )
    if not matching_module:
        return JsonResponse({"message": "Unknown PEAT module."}, status=404)

    config_yaml = generate_simple_config(
        [("127.0.0.1", "default-options-preview", matching_module)]
    )
    config_data = yaml.safe_load(config_yaml) or {}
    hosts = config_data.get("hosts") or []
    options = hosts[0].get("options", {}) if hosts else {}
    return JsonResponse(convert_tuples_to_dict(options or {}), safe=False)


def convert_tuples_to_dict(data):
    """Converts python tuples to dicts because YAML doesn't understand what that is."""
    if isinstance(data, list):
        # If the data is a list, iterate through its elements
        return [convert_tuples_to_dict(item) for item in data]
    elif isinstance(data, tuple):
        # If the data is a tuple, convert it to a dictionary
        if len(data) == 2:
            return {data[0]: data[1]}  # Convert tuple to dict with key-value pair
        else:
            return list(data)  # If tuple has more than 2 elements, return as list
    elif isinstance(data, dict):
        # If the data is a dictionary, recursively process its values
        return {key: convert_tuples_to_dict(value) for key, value in data.items()}
    else:
        # If it's neither a list, tuple, nor dict, return it as is
        return data


def get_run_objects(request, method):
    data = json.loads(request.body or "{}")
    pk = data.get("config")
    default_only = data.get("default_only", False)
    peat_run_query = _run_model(method).objects.all()

    if pk:
        peat_run_query = peat_run_query.filter(config_id=pk)
    elif default_only:
        peat_run_query = peat_run_query.filter(config__isnull=True)

    peat_run_objs = list(peat_run_query.order_by("-created").values())
    return JsonResponse(peat_run_objs, safe=False)


def peatScanConfigView(request):
    return render(request, "peat/peat_scan.html", {"peat_modules": module_api.names})


def clean_directory_name(name):
    allowed_characters = re.compile(r"[^a-zA-Z0-9_\- ]")
    cleaned_name = allowed_characters.sub("_", name or "")
    cleaned_name = cleaned_name.strip()
    return cleaned_name


def _split_names(value):
    """Return stripped, non-empty items from a list or comma/newline string."""
    raw_items = value if isinstance(value, list) else re.split(r"[\n,]+", str(value or ""))
    return [str(item).strip() for item in raw_items if str(item).strip()]


def parse_default_targets(targets):
    """Return a normalized target list from default-run form/API input."""
    return _split_names(targets)


def get_peat_run_name(method, requested_name):
    """Return the user-provided run name or a unique default run name."""
    cleaned_name = clean_directory_name(requested_name)
    if cleaned_name:
        return cleaned_name
    return f"peat_{method}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S%f')}"


def _format_device_target(device, target):
    return f"{device.label}: {target}" if device.label else str(target)


def _lookup_modules(raw_modules):
    """Return sorted PEAT module names; raise ValidationError for unknown ones."""
    selected_modules = set()
    invalid_modules = []
    for module in raw_modules:
        module_names = module_api.lookup_names([module])
        if not module_names:
            invalid_modules.append(module)
        selected_modules.update(module_names)

    if invalid_modules:
        invalid_list = ", ".join(invalid_modules)
        raise ValidationError(f"Unknown PEAT module selection: {invalid_list}")
    return sorted(selected_modules)


def parse_default_modules(modules):
    """Return normalized PEAT module names selected for a default run."""
    if modules is None:
        return ["all"]
    raw_modules = _split_names(modules)
    if not raw_modules or any(module.lower() == "all" for module in raw_modules):
        return ["all"]
    return _lookup_modules(raw_modules)


def _device_target(device):
    """Return ``(target, mode)`` PEAT can use for a device, or None."""
    for value, mode in (
        (device.ip, "ip"),
        (device.hostname, "ip"),
        (device.serial_port, "serial"),
    ):
        if value:
            return str(value), mode
    return None


def get_selected_config_targets(config_pk, device_ids):
    """Return display labels, targets, and PEAT target mode for selected devices.

    PEAT's CLI accepts network targets through ``-i`` and serial targets through
    ``-s``. A single PEAT invocation cannot mix those mutually-exclusive modes,
    so mixed selections are rejected instead of silently dropping hosts.
    """
    device_list = []
    network_targets = []
    serial_targets = []
    unsupported_devices = []

    peat_devices = PeatDevice.objects.filter(config_id=config_pk, id__in=device_ids)
    for device in peat_devices:
        classified = _device_target(device)
        if classified is None:
            unsupported_devices.append(str(device.label or device.pk))
            continue
        target, mode = classified
        device_list.append(_format_device_target(device, target))
        (serial_targets if mode == "serial" else network_targets).append(target)

    if unsupported_devices:
        raise PeatTargetSelectionError(
            "Selected host(s) do not have an IP address, hostname, or serial "
            "port PEAT can target from the webapp: "
            + ", ".join(unsupported_devices)
        )
    if network_targets and serial_targets:
        raise PeatTargetSelectionError(
            "Selected hosts mix network targets and serial targets. Run network "
            "and serial hosts separately."
        )

    if serial_targets:
        return device_list, serial_targets, "serial"
    return device_list, network_targets, "ip"


def _format_busy_lock_message(busy_targets):
    """Return a concise message for targets locked by another PEAT run."""
    displayed_targets = busy_targets[:10]
    target_list = ", ".join(displayed_targets)
    if len(busy_targets) > len(displayed_targets):
        target_list += f", and {len(busy_targets) - len(displayed_targets)} more"
    return f"PEAT target(s) are already in use: {target_list}"


def _busy_lock_response(busy_targets):
    """Return a response body that the frontend can use to prompt queueing."""
    return {
        "status": False,
        "reason": "targets_busy",
        "can_queue": True,
        "message": _format_busy_lock_message(busy_targets),
        "busy_targets": busy_targets,
    }


def _format_busy_target_display(display_target, requested_target, resolved_target):
    """Return a user-facing busy target label without hiding lock identity."""
    display_target = str(display_target)
    requested_target = str(requested_target)
    resolved_target = str(resolved_target)
    if resolved_target in display_target:
        return display_target
    if requested_target and requested_target != resolved_target:
        return f"{display_target} ({resolved_target})"
    return display_target


def _build_busy_target_display_map(display_targets, targets, target_mode):
    """Map concrete lock targets back to the labels shown in config-based runs."""
    display_by_target = {}
    for display_target, requested_target in zip(display_targets, targets):
        for resolved_target in resolve_lock_targets([requested_target], target_mode):
            display_by_target.setdefault(
                resolved_target.target,
                _format_busy_target_display(
                    display_target,
                    requested_target,
                    resolved_target.target,
                ),
            )
    return display_by_target


def _display_busy_targets(busy_targets, display_by_target):
    """Return busy targets with configured device labels when available."""
    return [display_by_target.get(target, target) for target in busy_targets]


def _resolve_peat_targets_for_queue(targets, target_mode):
    """Return concrete target strings to keep queued PEAT runs deterministic."""
    return [target.target for target in resolve_lock_targets(targets, target_mode)]


def _release_peat_locks_safely(owner):
    """Release Redis PEAT locks without masking the original view error."""
    if not owner:
        return
    try:
        release_owner_locks(owner)
    except Exception:
        LOGGER.exception("Failed to release PEAT locks for %s", owner)


def _delete_unscheduled_run(peat_run_obj):
    """Delete a run object that was saved only to derive a lock owner token."""
    if not peat_run_obj.pk:
        return
    try:
        peat_run_obj.delete()
    except Exception:
        LOGGER.exception("Failed to delete unscheduled PEAT run %s", peat_run_obj.pk)


def _load_json_file(path: Path):
    try:
        with path.open(encoding="utf-8") as file_obj:
            return json.load(file_obj)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _human_file_size(size):
    try:
        size = int(size)
    except (TypeError, ValueError):
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value = value / 1024
    return f"{size} B"


def _build_run_file_url(method, run_pk, relative_path, *, download=False):
    query_params = {"path": relative_path}
    if download:
        query_params["download"] = "1"
    file_url = reverse("peat:peatRun-file", kwargs={"method": method, "pk": run_pk})
    return f"{file_url}?{urlencode(query_params)}"


def _with_run_file_urls(info, method, run_pk, relative_path):
    """Add view/download URLs to ``info`` when the run is identified."""
    if method and run_pk:
        info["view_url"] = _build_run_file_url(method, run_pk, relative_path)
        info["download_url"] = _build_run_file_url(
            method, run_pk, relative_path, download=True
        )
    return info


def _safe_relative_file(root: Path, file_path: Path, method=None, run_pk=None):
    try:
        resolved = file_path.resolve()
        resolved.relative_to(root)
        stat = resolved.stat()
    except (FileNotFoundError, OSError, ValueError):
        return None
    relative_path = resolved.relative_to(root).as_posix()
    file_info = {
        "path": relative_path,
        "name": resolved.name,
        "size": stat.st_size,
        "size_display": _human_file_size(stat.st_size),
        "modified": datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(timespec="seconds"),
    }
    return _with_run_file_urls(file_info, method, run_pk, relative_path)


def _first(*values, default=""):
    """Return the first truthy value, else ``default``."""
    return next((value for value in values if value), default)


def _dict_of(mapping, key):
    return mapping.get(key) or {}


# Device summary count name -> summary keys, first non-empty one wins.
_SUMMARY_COUNT_KEYS = {
    "services": ("service",),
    "interfaces": ("interface",),
    "events": ("event",),
    "registers": ("registers", "register"),
    "tags": ("tag",),
    "io": ("io",),
    "users": ("users",),
}
# Device summary count name -> key under summary["related"].
_RELATED_COUNT_KEYS = {
    "related_files": "files",
    "related_ips": "ip",
    "related_protocols": "protocols",
}
_DEVICE_PLAIN_FIELDS = (
    "type",
    "ip",
    "hostname",
    "mac",
    "serial_port",
    "serial_number",
    "part_number",
    "uptime",
    "run_mode",
    "status",
)


def _device_counts(summary, file_count):
    related = _dict_of(summary, "related")
    counts = {"files": file_count}
    for name, keys in _SUMMARY_COUNT_KEYS.items():
        counts[name] = len(_first(*(summary.get(key) for key in keys), default=[]))
    for name, key in _RELATED_COUNT_KEYS.items():
        counts[name] = len(related.get(key) or [])
    return counts


def _device_details(summary):
    """Return the nested description/os/firmware/hardware fields as flat text."""
    description = _dict_of(summary, "description")
    os_info = _dict_of(summary, "os")
    firmware = _dict_of(summary, "firmware")
    hardware = _dict_of(summary, "hardware")
    return {
        "description": _first(description.get("full"), description.get("product")),
        "vendor": _dict_of(description, "vendor").get("name", ""),
        "os": _first(os_info.get("full"), os_info.get("name")),
        "firmware": _first(firmware.get("version"), firmware.get("revision")),
        "hardware": _first(
            hardware.get("version"), hardware.get("revision"), hardware.get("id")
        ),
    }


def _list_device_files(device_dir: Path, root: Path, method, run_pk):
    return [
        file_info
        for file_info in (
            _safe_relative_file(root, file_path, method, run_pk)
            for file_path in sorted(device_dir.rglob("*"))
            if file_path.is_file() and not file_path.is_symlink()
        )
        if file_info
    ]


def _read_device_summary(device_dir: Path, root: Path, method=None, run_pk=None):
    summary = _load_json_file(device_dir / "device-data-summary.json")
    if not isinstance(summary, dict):
        return None

    files = _list_device_files(device_dir, root, method, run_pk)
    device_id = _first(
        summary.get("label"),
        summary.get("id"),
        summary.get("ip"),
        summary.get("hostname"),
        summary.get("serial_port"),
        default=device_dir.name,
    )
    return {
        "id": device_id,
        "directory": device_dir.relative_to(root).as_posix(),
        **{field: summary.get(field, "") for field in _DEVICE_PLAIN_FIELDS},
        **_device_details(summary),
        "counts": _device_counts(summary, len(files)),
        "top_files": files[:8],
    }


def _relative_to_root(path: Path, root: Path):
    """Return ``path`` relative to ``root``, or None for symlinks/escapes."""
    if path.is_symlink():
        return None
    try:
        return path.resolve().relative_to(root)
    except (OSError, ValueError):
        return None


def _tree_node(path: Path, relative_path: Path, method, run_pk):
    """Return the tree node for a directory or file, None for other entries."""
    relative_path_text = relative_path.as_posix()
    if path.is_dir():
        return {"path": relative_path_text, "name": path.name, "type": "dir"}
    if not path.is_file():
        return None
    size = path.stat().st_size
    node = {
        "path": relative_path_text,
        "name": path.name,
        "type": "file",
        "size": size,
        "size_display": _human_file_size(size),
    }
    return _with_run_file_urls(node, method, run_pk, relative_path_text)


def _build_run_file_tree(root: Path, method=None, run_pk=None, limit=None):
    nodes = []
    total_files = 0
    total_dirs = 0
    total_bytes = 0
    files_by_section = {}

    for path in sorted(root.rglob("*")):
        relative_path = _relative_to_root(path, root)
        if relative_path is None:
            continue
        node = _tree_node(path, relative_path, method, run_pk)
        if node is None:
            continue
        if node["type"] == "dir":
            total_dirs += 1
        else:
            total_files += 1
            total_bytes += node["size"]
            section = relative_path.parts[0] if relative_path.parts else "."
            files_by_section[section] = files_by_section.get(section, 0) + 1
        if limit is None or len(nodes) < limit:
            nodes.append(node)

    return {
        "nodes": nodes,
        "truncated": limit is not None and (total_files + total_dirs) > len(nodes),
        "total_files": total_files,
        "total_dirs": total_dirs,
        "total_size": total_bytes,
        "total_size_display": _human_file_size(total_bytes),
        "files_by_section": files_by_section,
    }


def _outcome(severity, title, message):
    return {"severity": severity, "title": title, "message": message}


_OUTCOME_NOT_AVAILABLE = _outcome(
    "info",
    "Run data is not available yet.",
    "PEAT has not written a run directory for this job yet.",
)
_OUTCOME_WAITING = _outcome(
    "info",
    "Run is waiting for target locks.",
    "PEAT will start this run when the selected targets are no longer in use.",
)
_OUTCOME_BY_RUN_DIR_ERROR = {
    "invalid": _outcome(
        "danger",
        "Invalid run directory.",
        "The saved run directory is outside the expected PEAT output location.",
    ),
    "missing": _outcome(
        "warning",
        "Run files are not available.",
        "The database has a run directory name, but the directory is not "
        "present on disk.",
    ),
}
_OUTCOME_BY_STATUS = {
    "error": _outcome(
        "danger",
        "Run ended with an error.",
        "Some files may still be useful. Check the console, PEAT logs, and "
        "any partial device folders below.",
    ),
    "stopped": _outcome(
        "warning",
        "Run was stopped.",
        "Partial output may be available. Download the run or inspect the "
        "files listed below.",
    ),
    "running": _outcome(
        "info",
        "Run is still in progress.",
        "Summary information will continue to update as PEAT writes files.",
    ),
}
_OUTCOME_BY_STATUS["started"] = _OUTCOME_BY_STATUS["running"]
_OUTCOME_SUCCESS = _outcome(
    "success",
    "Run completed with device data.",
    "Use the device cards, important files, and directory map to decide what "
    "to inspect or compare with future runs.",
)
_OUTCOME_NO_DEVICES_IDENTIFIED = _outcome(
    "warning",
    "No devices were identified.",
    "PEAT completed the scan workflow but did not positively identify any "
    "supported devices. Review targets, reachability, selected modules, and "
    "the console log before treating this as an application failure.",
)
_COMPLETED_STATUSES = {"done", "error", "stopped"}


def _run_outcome(status, method, device_count):
    """Return the user-facing outcome banner for a run with output on disk."""
    if device_count == 0 and status in _COMPLETED_STATUSES:
        if status == "error" and method == "scan":
            return _OUTCOME_NO_DEVICES_IDENTIFIED
        return _outcome(
            "warning" if method == "scan" else "info",
            "No devices found in this run.",
            "The run produced files, but no device-level summaries were found. "
            "This can be a valid scan result when no hosts match PEAT's device "
            "fingerprints.",
        )
    return _OUTCOME_BY_STATUS.get(status, _OUTCOME_SUCCESS)


def _run_parent_dir(method):
    parent = settings.PEAT_PULL_DIR if method == "pull" else settings.PEAT_SCAN_DIR
    return Path(parent).resolve()


def _resolve_run_root(method, run_dir):
    """Return ``(root, error)``; error is "invalid", "missing" or None."""
    parent_run_dir = _run_parent_dir(method)
    root = (parent_run_dir / run_dir).resolve()
    if not root.is_relative_to(parent_run_dir):
        return None, "invalid"
    if not root.is_dir():
        return None, "missing"
    return root, None


def _important_files(root: Path, method, run_pk):
    relatives = [
        Path("summaries") / f"{method}-summary.json",
        Path("logs") / "console.log",
        Path("logs") / "peat.log",
        Path("logs") / "json-log.jsonl",
        Path("logs") / "debug-info.txt",
        Path("peat_metadata") / "peat_configuration.yaml",
        Path("peat_metadata") / "peat_state.json",
    ]
    infos = (_safe_relative_file(root, root / rel, method, run_pk) for rel in relatives)
    return [info for info in infos if info]


def _find_device_roots(root: Path):
    devices_dir = root / "devices"
    if devices_dir.is_dir():
        return [path for path in sorted(devices_dir.iterdir()) if path.is_dir()]
    if (root / "device-data-summary.json").is_file():
        return [root]
    return []


def _read_device_summaries(root: Path, method, run_pk):
    summaries = (
        _read_device_summary(device_dir, root, method, run_pk)
        for device_dir in _find_device_roots(root)
    )
    return [summary for summary in summaries if summary]


def _summary_device_count(summary, method):
    if not isinstance(summary, dict):
        return 0
    key = "pull_results" if method == "pull" else "hosts_verified"
    return len(summary.get(key, []) or [])


def _run_counts(obj, tree, summary, device_count):
    files_by_section = tree.get("files_by_section", {})
    summary = summary if isinstance(summary, dict) else {}
    return {
        "targets": len(obj.devices or []),
        "devices": device_count,
        "files": tree.get("total_files", 0),
        "directories": tree.get("total_dirs", 0),
        "logs": files_by_section.get("logs", 0),
        "summaries": files_by_section.get("summaries", 0),
        "metadata": files_by_section.get("peat_metadata", 0),
        "elastic": files_by_section.get("elastic_data", 0),
        "hosts_online": summary.get("num_hosts_online"),
        "hosts_verified": summary.get("num_hosts_verified"),
    }


def _get_peat_run_details(obj, method):
    run_dir = obj.run_dir
    response = {
        "method": method,
        "status": obj.status,
        "run_dir": run_dir,
        "name": obj.name,
        "created": obj.created.isoformat() if obj.created else None,
        "targets": obj.devices or [],
        "modules": obj.modules or [],
        "config": {"id": obj.config_id, "name": obj.config.name} if obj.config else None,
        "output_exists": False,
        "summary_available": False,
        "devices": [],
        "tree": {"nodes": [], "total_files": 0, "total_dirs": 0, "total_size_display": "0 B"},
        "important_files": [],
        "counts": {},
        "outcome": dict(_OUTCOME_NOT_AVAILABLE),
    }
    if not run_dir:
        return response
    if obj.status == "waiting":
        response["outcome"] = dict(_OUTCOME_WAITING)
        return response

    root, error = _resolve_run_root(method, run_dir)
    if error:
        response["outcome"] = dict(_OUTCOME_BY_RUN_DIR_ERROR[error])
        return response

    summary = _load_json_file(root / "summaries" / f"{method}-summary.json")
    tree = _build_run_file_tree(root, method, obj.pk)
    devices = _read_device_summaries(root, method, obj.pk)
    device_count = max(len(devices), _summary_device_count(summary, method))
    response.update(
        output_exists=True,
        summary_available=isinstance(summary, dict),
        tree=tree,
        important_files=_important_files(root, method, obj.pk),
        devices=devices,
        counts=_run_counts(obj, tree, summary, device_count),
        outcome=dict(_run_outcome(obj.status, method, device_count)),
    )
    return response


class PeatConfigListView(ListView):
    """Django template to handle context and rendering all peat configs
    we have stored."""

    model = PeatConfig
    template_name = "peat/peatconfig_list.html"
    paginate_by = 10
    ordering = ["-updated"]

    def delete(self, request):
        body_dict = json.loads(request.body)
        ids = body_dict.get("ids")
        PeatConfig.objects.filter(id__in=ids).delete()
        return JsonResponse({"message": "success"}, status=204)


class PeatConfigDetailView(DetailView):
    """Django template to handle context and rendering details of a
    peat config."""

    model = PeatConfig
    template_name = "peat/peatconfig_detail.html"

    def post(self, request):
        peat_config = self.get_object()
        peat_pulls = list(peat_config.pulls.all().values())
        return JsonResponse(peat_pulls, safe=False)


class PeatConfigCreateView(CreateView):
    """Django renderer and handler to create a new peat config based
    on the PeatConfigFrom."""

    template_name = "peat/peatconfig_form.html"
    model = PeatConfig
    form_class = PeatConfigForm

    def get_success_url(self):
        return reverse("peat:peatconfig-details", kwargs={"pk": self.object.pk})


class PeatBuilderView(CreateView):
    """Django renderer and handler to create a new peat config based
    on the PeatBuilderFrom."""

    template_name = "peat/peatconfig_build.html"
    model = PeatConfig
    form_class = PeatBuilderForm

    def get_success_url(self):
        return reverse("peat:peatconfig-details", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        device_names = self.request.POST.getlist("device-name")
        device_ips = self.request.POST.getlist("device-ip")
        device_modules = self.request.POST.getlist("device-module")
        device_options = self.request.POST.getlist("device-options")
        devices = [
            (device_ips[i], device_names[i], device_modules[i])
            for i in range(len(device_names))
        ]

        config_yaml = generate_simple_config(devices)
        config_data = yaml.safe_load(config_yaml) or {}
        hosts = config_data.get("hosts", [])
        for index, raw_options in enumerate(device_options):
            raw_options = raw_options.strip()
            if not raw_options:
                continue
            try:
                options = json.loads(raw_options)
            except json.JSONDecodeError:
                form.add_error(None, f"Device {index + 1} has invalid JSON options.")
                return self.form_invalid(form)
            if index < len(hosts):
                hosts[index]["options"] = options
        config_yaml = yaml.dump(config_data, default_flow_style=False, sort_keys=False)

        config_filename = "autobuilt_config_" + str(datetime.now(timezone.utc).date()) + ".yml"
        config_file = ContentFile(config_yaml.encode("utf-8"), name=config_filename)

        instance = form.save(commit=False)
        instance.filename.save(config_filename, config_file, save=False)
        instance.save()

        return super().form_valid(form)


class PeatConfigUpdateView(UpdateView):
    """Django renderer and handler to create a new peat config based
    on the PeatConfigFrom."""

    template_name = "peat/peatconfig_form.html"
    model = PeatConfig
    form_class = PeatConfigForm

    def get_success_url(self):
        # Redirect to the detail view of the updated instance
        return reverse("peat:peatconfig-details", kwargs={"pk": self.object.pk})


class PeatDeviceUpdateView(UpdateView):
    """Django renderer and handler to update a device details based
    on the PeatDeviceFrom."""

    template_name = "peat/peatdevice_form.html"
    model = PeatDevice
    form_class = PeatDeviceForm

    def get_success_url(self):
        # Redirect to the detail view of the updated instance
        return reverse("peat:peatconfig-details", kwargs={"pk": self.object.config.pk})


class PeatRunView(DetailView):
    """Django super class of selected peat pull or scan."""

    def get(self, request):
        search_pk = request.GET.get("pk", "")
        obj = get_object_or_404(self.model, pk=search_pk)
        method = self.model.get_method()
        if method not in {"pull", "scan"}:
            raise Http404("method is not pull or scan")
        return JsonResponse(_get_peat_run_details(obj, method), safe=False)

    def delete(self, request):
        body_dict = json.loads(request.body)
        ids = body_dict.get("ids")
        if ids:
            self.model.objects.filter(id__in=ids).delete()
        return JsonResponse({"message": "success"}, status=204)


class _RunRequestError(Exception):
    """Carries the JSON response for a rejected pull/scan request."""

    def __init__(self, response):
        super().__init__()
        self.response = response


class _RunPlan(NamedTuple):
    name: str
    url: str | None
    config_pk: object
    peat_config: object
    device_list: list
    targets: list
    target_mode: str
    modules: list
    busy_target_display_map: dict
    queue_if_busy: bool


def _rejected(message):
    return _RunRequestError(JsonResponse({"status": False, "message": message}))


def _push_database_url(request, push):
    """Return the datastore URL for a push run, None when not pushing."""
    if not push:
        return None
    response = get_database_status(request=request)
    if not json.loads(response.content)["status"]:
        raise _RunRequestError(response)
    return get_database_url(request=request)


def _resolve_run_targets(data, config_pk, device_ids):
    """Return ``(device_list, targets, target_mode, modules)`` for a run."""
    if config_pk and device_ids:
        try:
            return (*get_selected_config_targets(config_pk, device_ids), [])
        except PeatTargetSelectionError as error:
            raise _rejected(str(error)) from error
    targets = parse_default_targets(data.get("cidr_id"))
    try:
        modules = parse_default_modules(data.get("device_types"))
    except ValidationError as error:
        raise _rejected("\n".join(error.messages)) from error
    return targets, targets, "ip", modules


def _plan_run(request, data, kind, fetch_config=False):
    """Validate a pull/scan request body; raise _RunRequestError to reject it."""
    config_pk = data.get("config")
    device_ids = data.get("device_ids", [])
    use_config = bool(config_pk and device_ids)
    name = get_peat_run_name(kind, data.get("peat_run_name"))
    url = _push_database_url(request, data.get("peat_run_push", False))
    peat_config = (
        PeatConfig.objects.get(pk=config_pk) if fetch_config and use_config else None
    )
    device_list, targets, target_mode, modules = _resolve_run_targets(
        data, config_pk, device_ids
    )
    if not targets:
        raise _rejected(f"Provide at least one host or CIDR target to {kind}.")
    busy_map = (
        _build_busy_target_display_map(device_list, targets, target_mode)
        if use_config
        else {}
    )
    return _RunPlan(
        name,
        url,
        config_pk,
        peat_config,
        device_list,
        targets,
        target_mode,
        modules,
        busy_map,
        data.get("queue_if_busy", False),
    )


# Trailing task args for runs queued to wait on target locks.
_QUEUED_TASK_ARGS = (True, False)


def _schedule_run(run_obj, task, plan, lock_owner, build_args):
    """Queue or start the Celery task; return the JSON response."""
    if plan.queue_if_busy:
        resolved = _resolve_peat_targets_for_queue(plan.targets, plan.target_mode)
        result = task.apply_async(
            build_args(resolved) + _QUEUED_TASK_ARGS, task_id=run_obj.task_id
        )
        return JsonResponse(
            {
                "status": True,
                "message": result.id,
                "pk": run_obj.pk,
                "queued_for_locks": True,
            }
        )

    lock_acquisition = acquire_target_locks(
        owner=lock_owner, targets=plan.targets, target_mode=plan.target_mode
    )
    if not lock_acquisition.acquired:
        _delete_unscheduled_run(run_obj)
        busy_targets = _display_busy_targets(
            lock_acquisition.busy_targets, plan.busy_target_display_map
        )
        return JsonResponse(_busy_lock_response(busy_targets))

    result = task.apply_async(
        build_args(lock_acquisition.targets), task_id=run_obj.task_id
    )
    return JsonResponse({"status": True, "message": result.id, "pk": run_obj.pk})


def _dispatch_run(run_obj, task, kind, plan, build_args):
    """Save the run, take locks and hand it to Celery; always return JSON."""
    lock_owner = None
    try:
        run_obj.full_clean()
        if plan.queue_if_busy:
            run_obj.status = "waiting"
        run_obj.save()
        lock_owner = build_owner_token_for_run(run_obj)
        return _schedule_run(run_obj, task, plan, lock_owner, build_args)
    except ValidationError as error:
        return JsonResponse({"status": False, "message": "\n".join(error.messages)})
    except PeatLockError as error:
        LOGGER.info("Failed to acquire PEAT %s locks: %s", kind, error)
        _delete_unscheduled_run(run_obj)
        return JsonResponse({"status": False, "message": str(error)})
    except Exception as error:
        LOGGER.exception("Failed to queue PEAT %s", kind)
        _release_peat_locks_safely(lock_owner)
        if run_obj.pk:
            run_obj.status = "error"
            run_obj.save(update_fields=["status"])
            run_obj._append_console_message(
                f"Failed to queue PEAT {kind} task: {error}"
            )
        return JsonResponse(
            {"status": False, "message": f"Unable to queue PEAT {kind} task."}
        )


class PeatPullView(PeatRunView):
    """Django handler to create a new peat pull and signal celery to start the pull asynchronously
    using the peat interface."""

    model = PeatPull
    template_name = "peat/peatpull_detail.html"

    def post(self, request):
        data = json.loads(request.body)
        try:
            plan = _plan_run(request, data, "pull", fetch_config=True)
        except _RunRequestError as error:
            return error.response
        peat_pull_obj = PeatPull(
            name=plan.name,
            baseline=data.get("peat_run_baseline", False),
            devices=plan.device_list,
            run_dir=plan.name,
            modules=plan.modules,
            config=plan.peat_config,
            task_id=str(uuid.uuid4()),
        )
        return _dispatch_run(
            peat_pull_obj,
            peat_pull,
            "pull",
            plan,
            lambda targets: (peat_pull_obj.pk, targets, plan.url, plan.target_mode),
        )


class PeatScanView(PeatRunView):
    """
    Django handler to create a new peat scan and signal celery to start
    the scan asynchronously using the peat interface.
    """

    model = PeatScan
    template_name = "peat/peat_scan.html"

    def post(self, request):
        data = json.loads(request.body)
        try:
            plan = _plan_run(request, data, "scan")
        except _RunRequestError as error:
            return error.response
        intensive_scan = data.get("intensive_scan", False)
        peat_scan_obj = PeatScan(
            name=plan.name,
            devices=plan.device_list,
            run_dir=plan.name,
            modules=plan.modules,
            task_id=str(uuid.uuid4()),
            config_id=plan.config_pk or None,
        )
        return _dispatch_run(
            peat_scan_obj,
            peat_scan,
            "scan",
            plan,
            lambda targets: (
                peat_scan_obj.pk,
                targets,
                plan.url,
                plan.config_pk,
                intensive_scan,
                plan.target_mode,
            ),
        )


class PeatPullDetailView(DetailView):
    """Django template to handle context and rendering details of a
    selected peat pull or scan."""

    model = PeatPull
    template_name = "peat/run_details.html"


class PeatScanDetailView(DetailView):
    """Django template to handle context and rendering details of a
    selected peat pull or scan."""

    model = PeatScan
    template_name = "peat/run_details.html"


class PeatResultsView(View):

    def get(self, request):
        """Returns json list of all folders in peat pull directory"""
        from .data_source import list_pull_datasets

        folders = [dataset["id"] for dataset in list_pull_datasets()]
        return JsonResponse(folders, safe=False)

    def post(self, request):
        """Returns status of an individual peat run"""
        data, bad_request = _json_body(request)
        if bad_request:
            return bad_request
        obj = _get_run_object(data.get("method"), data.get("pk"))
        return JsonResponse({"status": obj.status, "run_dir": obj.run_dir}, safe=False)


_RUN_MODELS = {"pull": PeatPull, "scan": PeatScan}
_VIEWABLE_SUFFIXES = frozenset(
    {".conf", ".csv", ".ini", ".json", ".jsonl", ".log", ".md", ".txt", ".yaml", ".yml"}
)


def _run_model(method):
    try:
        return _RUN_MODELS[method]
    except (KeyError, TypeError):
        raise Http404("method is not pull or scan") from None


def _get_run_object(method, pk):
    return get_object_or_404(_run_model(method), pk=pk)


def _json_body(request):
    """Return ``(data, None)`` or ``(None, 400 response)`` for a JSON body."""
    try:
        return json.loads(request.body or "{}"), None
    except json.JSONDecodeError:
        return None, JsonResponse({"message": "Invalid request body."}, status=400)


def _get_run_file_context(method, pk):
    obj = _get_run_object(method, pk)
    if not obj.run_dir:
        raise Http404("Run files are not available yet.")
    root, error = _resolve_run_root(method, obj.run_dir)
    if error == "invalid":
        raise Http404("Invalid run directory.")
    if error:
        raise Http404("Run files are not available.")
    return obj, root


def _run_file_content_type(file_path, as_attachment):
    content_type, _ = mimetypes.guess_type(file_path.name)
    if not as_attachment and file_path.suffix.lower() not in _VIEWABLE_SUFFIXES:
        return "application/octet-stream"
    return content_type or (
        "application/octet-stream" if as_attachment else "text/plain"
    )


@require_GET
def peat_run_file(request, method, pk):
    _, root = _get_run_file_context(method, pk)
    relative_path = request.GET.get("path", "")
    if not relative_path:
        raise Http404("File path is required.")

    file_path = (root / relative_path).resolve()
    if not file_path.is_relative_to(root):
        raise Http404("Invalid file path.")
    if not file_path.is_file() or file_path.is_symlink():
        raise Http404("File is not available.")

    as_attachment = request.GET.get("download") == "1"
    response = FileResponse(
        file_path.open("rb"),
        as_attachment=as_attachment,
        filename=file_path.name,
        content_type=_run_file_content_type(file_path, as_attachment),
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_POST
def peat_run_stop(request):
    data, bad_request = _json_body(request)
    if bad_request:
        return bad_request
    obj = _get_run_object(data.get("pull_or_scan", ""), data.get("pk", ""))
    obj.stop()
    obj.refresh_from_db(fields=["status"])
    return JsonResponse({"message": "success", "status": obj.status}, status=200)


def _collect_run_files(folder_path: Path):
    return [
        file_path
        for file_path in folder_path.rglob("*")
        if file_path.is_file()
        and not file_path.is_symlink()
        and file_path.resolve().is_relative_to(folder_path)
    ]


def _zip_run_response(folder_path: Path, run_files, run_dir):
    download_name = clean_directory_name(run_dir) or "peat-run"
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for file_path in run_files:
            zip_file.write(file_path, file_path.relative_to(folder_path))
    zip_buffer.seek(0)
    response = HttpResponse(zip_buffer, content_type="application/zip")
    response["Content-Disposition"] = f"attachment; filename={download_name}.zip"
    return response


@require_POST
def peat_run_download(request):
    data, bad_request = _json_body(request)
    if bad_request:
        return bad_request

    method = data.get("pull_or_scan", "")
    obj = _get_run_object(method, data.get("pk", ""))
    if not obj.run_dir:
        return JsonResponse({"message": "Run files are not available yet."}, status=404)

    folder_path, error = _resolve_run_root(method, obj.run_dir)
    if error == "invalid":
        return JsonResponse({"message": "Invalid run directory."}, status=400)
    run_files = [] if error else _collect_run_files(folder_path)
    if not run_files:
        return JsonResponse({"message": "Run files are not available."}, status=404)
    return _zip_run_response(folder_path, run_files, obj.run_dir)

