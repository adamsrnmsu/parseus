import logging
import os
import shutil
import tempfile
from pathlib import Path

import yaml
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from kombu.exceptions import OperationalError

from parseus.webapp.project.celery import app

from .peat_locks import build_owner_token_for_run, release_owner_locks
from .peat_process import current_process_host, terminate_process_group


def validate_yaml_file(uploaded_file):
    try:
        # Attempt to load the YAML file
        yaml.safe_load(uploaded_file.read())
    except yaml.YAMLError:
        raise ValidationError(f"Invalid YAML file: {uploaded_file}")
    finally:
        uploaded_file.seek(0)  # Reset file pointer to the beginning


ACTIVE_STATUSES = {"started", "waiting", "running"}


def _revoke_task(task_id):
    """Revoke a queued Celery task without terminating the worker process."""
    try:
        app.control.revoke(task_id, terminate=False)
    except OperationalError:
        logging.exception(
            "RabbitMQ not running. Running/Scheduled tasks may not be "
            "properly removed."
        )


def _is_remote_process(process_id, process_host) -> bool:
    """Return True when the PID belongs to another PID namespace than ours."""
    return bool(process_id and process_host and process_host != current_process_host())


def _terminate_local_process(process_id) -> bool:
    """Terminate a local process group; True when its tree is known to be gone."""
    try:
        terminate_process_group(int(process_id))
    except ProcessLookupError:
        logging.debug("No process running: %s", process_id)
    except OSError:
        logging.exception("Failed to terminate PEAT process %s", process_id)
        return False
    return True


def _should_release_locks(previous_status, had_process_id, process_tree_finished) -> bool:
    """Decide whether a stopped run's locks can be released right away."""
    return (
        previous_status == "started"
        or previous_status not in ACTIVE_STATUSES
        or (had_process_id and process_tree_finished)
    )


class PeatConfig(models.Model):
    filename = models.FileField(upload_to="peat", validators=[validate_yaml_file])
    name = models.CharField(max_length=150, blank=True, null=True)
    description = models.CharField(max_length=255, blank=True, null=True)
    author = models.CharField(max_length=150, blank=True, null=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    assume_online = models.BooleanField(default=False)
    intensive_scan = models.BooleanField(default=False)
    debug = models.IntegerField(default=0, choices=[(i, str(i)) for i in range(5)])

    def __str__(self):
        return f"{self.name}"

    def _set_hosts(self):
        with open(self.filename.path, encoding="utf-8") as peat_config:
            data = yaml.safe_load(peat_config) or {}
        hosts = data.get("hosts", [])
        for host in hosts:
            identifiers: dict = host["identifiers"]
            PeatDevice.objects.create(
                label=host["label"],
                comment=host.get("comment", ""),
                ip=identifiers.get("ip", None),
                hostname=identifiers.get("hostname", None),
                mac=identifiers.get("mac", None),
                serial_port=identifiers.get("serial_port", None),
                name=identifiers.get("name", None),
                peat_module=host["peat_module"],
                options=host.get("options", None),
                config=self,
            )

    def _update_metadata(self, data: dict):
        metadata = {}
        metadata["name"] = self.name
        metadata["description"] = self.description
        metadata["author"] = self.author
        metadata["created"] = self.created.strftime("%Y-%m-%d %H:%M:%S")
        metadata["updated"] = self.updated.strftime("%Y-%m-%d %H:%M:%S")
        data["metadata"] = metadata

    def _update_config_options(self, data: dict):
        data["debug"] = self.debug
        data["assume_online"] = self.assume_online
        data["intensive_scan"] = self.intensive_scan

    def _update_hosts(self, data: dict):
        if not self.hosts.exists():
            data.pop("hosts", None)
            return
        hosts = []
        for host in self.hosts.all():
            hosts.append(host.get_config_data())
        data["hosts"] = hosts

    def _update_config_file(self):
        with open(self.filename.path, encoding="utf-8") as peat_config:
            data = yaml.safe_load(peat_config) or {}

        self._update_metadata(data)
        self._update_config_options(data)
        self._update_hosts(data)

        # Create a temporary file
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=".yaml", mode="w"
        ) as temp_file:
            # Write data to the temporary file
            yaml.dump(data, temp_file, default_flow_style=False, sort_keys=False)
            temp_file_path = temp_file.name  # Get the name of the temporary file

        # Replace the target file if it exists
        if os.path.exists(self.filename.path):
            os.remove(self.filename.path)  # Remove the existing file

        # Move the temporary file to the target file path
        shutil.move(temp_file_path, self.filename.path)
        current_umask = os.umask(0)  # Temporarily set umask to 0
        os.umask(current_umask)  # Immediately restore
        base_permissions = 0o666  # Base permissions for a file
        effective_permissions = base_permissions & ~current_umask
        os.chmod(self.filename.path, effective_permissions)


class PeatDevice(models.Model):
    label = models.CharField(max_length=150)
    comment = models.CharField(max_length=255, null=True)
    ip = models.GenericIPAddressField(
        protocol="IPv4", unpack_ipv4=False, blank=True, null=True
    )
    hostname = models.CharField(max_length=150, blank=True, null=True)
    mac = models.CharField(max_length=17, blank=True, null=True)
    serial_port = models.CharField(max_length=150, blank=True, null=True)
    name = models.CharField(max_length=150, blank=True, null=True)
    peat_module = models.CharField(max_length=150, blank=True, null=True)
    options = models.JSONField(default=dict, null=True)
    config = models.ForeignKey(
        PeatConfig, on_delete=models.CASCADE, related_name="hosts"
    )

    def __str__(self):
        return f"{self.label}"

    def _get_identifiers(self):
        identifiers = {}
        if self.ip:
            identifiers["ip"] = self.ip
        if self.hostname:
            identifiers["hostname"] = self.hostname
        if self.mac:
            identifiers["mac"] = self.mac
        if self.serial_port:
            identifiers["serial_port"] = self.serial_port
        if self.name:
            identifiers["name"] = self.name
        return identifiers

    def get_config_data(self):
        config = {}
        config["label"] = self.label
        config["comment"] = self.comment
        config["identifiers"] = self._get_identifiers()
        config["peat_module"] = self.peat_module
        if self.options:
            config["options"] = self.options
        return config


def _mark_stopped(run):
    run.status = "stopped"
    run.save(update_fields=["status"])
    run._append_console_message("Stop requested by user; terminating PEAT run...")
    # Revoke queued tasks without terminating the Celery worker process.
    # Running PEAT work is canceled by terminating the isolated subprocess
    # group, allowing the worker task to clean up DB state.
    if run.task_id:
        _revoke_task(run.task_id)


def _terminate_process(run):
    """Return ``(process_is_remote, process_tree_finished)`` after terminating."""
    process_is_remote = _is_remote_process(run.process_id, run.process_host)
    if process_is_remote:
        # The PID belongs to another container's PID namespace; signalling it
        # here could kill an unrelated local process. The owning worker
        # watches for the "stopped" status and terminates its own subprocess,
        # then releases the locks in its task cleanup.
        logging.info(
            "PEAT process %s belongs to PID namespace %s; leaving "
            "termination to that worker",
            run.process_id,
            run.process_host,
        )
        return True, False
    if run.process_id:
        return False, _terminate_local_process(run.process_id)
    return False, False


class PeatRun(models.Model):
    STATUS_CHOICES = [
        ("started", "Started"),
        ("waiting", "Waiting"),
        ("error", "Error"),
        ("running", "Running"),
        ("done", "Done"),
        ("stopped", "Stopped"),
    ]
    name = models.CharField(max_length=255, null=True, blank=True, unique=True)
    created = models.DateTimeField(auto_now_add=True)
    devices = models.JSONField(default=list)
    modules = models.JSONField(default=list, blank=True)
    run_dir = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="started")
    task_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True
    )  # Store the Celery task ID
    process_id = models.IntegerField(null=True, blank=True)  # Store the process ID
    # UUID of the PID namespace (container/host) that process_id belongs to.
    process_host = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        abstract = True  # This makes the class abstract

    def get_type(self):
        return self.__class__.__name__

    def __str__(self):
        return f"Directory: {self.run_dir}; Status: {self.status}"

    def _append_console_message(self, message):
        """Write a status message into the live PEAT console log, if available."""
        if not self.run_dir:
            return

        if self.get_method() == "pull":
            parent_run_dir = settings.PEAT_PULL_DIR
        elif self.get_method() == "scan":
            parent_run_dir = settings.PEAT_SCAN_DIR
        else:
            return

        parent_run_dir = Path(parent_run_dir).resolve()
        log_dir = (parent_run_dir / self.run_dir / "logs").resolve()
        try:
            log_dir.relative_to(parent_run_dir)
        except ValueError:
            return

        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "console.log").open("a", encoding="utf-8", buffering=1) as f:
            f.write(f"\n{message}\n")

    def stop(self):
        """Stop the queued/running PEAT task and its subprocess tree."""
        self.refresh_from_db(
            fields=["status", "task_id", "process_id", "process_host", "run_dir"]
        )
        previous_status = self.status
        should_mark_stopped = previous_status in ACTIVE_STATUSES

        if should_mark_stopped:
            _mark_stopped(self)

        process_is_remote, process_tree_finished = _terminate_process(self)
        if self.task_id and _should_release_locks(
            previous_status, bool(self.process_id), process_tree_finished
        ):
            try:
                release_owner_locks(build_owner_token_for_run(self))
            except Exception:
                logging.exception("Failed to release PEAT target locks")

        if not process_is_remote:
            self.process_id = None
            self.process_host = None
            self.save(update_fields=["process_id", "process_host"])
        if should_mark_stopped:
            self._append_console_message("PEAT run stopped.")


class PeatPull(PeatRun):
    baseline = models.BooleanField(default=False)
    config = models.ForeignKey(
        PeatConfig,
        on_delete=models.CASCADE,
        related_name="pulls",
        blank=True,
        null=True,
    )

    @classmethod
    def get_method(cls):
        return "pull"


class PeatScan(PeatRun):
    config = models.ForeignKey(
        PeatConfig,
        on_delete=models.CASCADE,
        related_name="scans",
        blank=True,
        null=True,
    )

    @classmethod
    def get_method(cls):
        return "scan"
