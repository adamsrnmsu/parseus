from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Dict, List

from manage_parseus_lib.config.model import InstallerConfig
from manage_parseus_lib.constants import LONG_CMD_TIMEOUT
from manage_parseus_lib.docker.compose import (
    bring_up_stack,
    compose_runtime_cmd,
    compute_named_volumes,
    compute_required_images,
    get_rendered_compose_config,
    show_stack_status,
    stack_has_running_containers,
    validate_compose_files,
    verify_required_bind_mounts,
    verify_required_ports,
)
from manage_parseus_lib.docker.images import verify_required_images
from manage_parseus_lib.install.env_files import parse_env_file
from manage_parseus_lib.install.certificates import uses_external_tls_certificates
from manage_parseus_lib.install.paths import InstallPaths
from manage_parseus_lib.errors import ManageParseusError
from manage_parseus_lib.output import die, info, ok, warn
from manage_parseus_lib.process import run_streaming
from manage_parseus_lib.system.health import wait_for_https_ready, wait_for_port


@dataclass(frozen=True)
class StackPlan:
    """Validated Docker Compose plan for a Parseus stack operation."""

    rendered: Dict
    required_images: List[str]
    named_volumes: List[str]


def build_stack_plan(install_paths: InstallPaths, integrations: List[str]) -> StackPlan:
    validate_compose_files(install_paths, integrations)
    rendered = get_rendered_compose_config(install_paths, integrations)
    required_images = compute_required_images(rendered)
    named_volumes = compute_named_volumes(rendered)
    return StackPlan(
        rendered=rendered,
        required_images=required_images,
        named_volumes=named_volumes,
    )


def verify_zeek_interface(install_paths: InstallPaths) -> None:
    """The zeek service runs on the host network, so ZEEK_IFACE must exist on the host."""
    env = parse_env_file(install_paths.env_dir / "ingestor.env")
    iface = env.get("ZEEK_IFACE") or "eth0"  # entrypoint.sh default
    names = [name for _, name in socket.if_nameindex()]
    if iface not in names:
        die(
            f"ZEEK_IFACE={iface} does not exist on this host; set it in "
            f"{install_paths.env_dir / 'ingestor.env'}. Interfaces: {', '.join(names)}"
        )


def verify_stack_plan(
    install_paths: InstallPaths,
    integrations: List[str],
    plan: StackPlan,
    *,
    skip_port_check_when_running: bool = False,
    verify_images: bool = True,
) -> None:
    if skip_port_check_when_running and stack_has_running_containers(
        install_paths, integrations
    ):
        info(
            "Existing stack containers are running; "
            "skipping host port availability check"
        )
    else:
        verify_required_ports(plan.rendered)
    verify_required_bind_mounts(plan.rendered)
    if "zeek" in integrations:
        verify_zeek_interface(install_paths)
    if verify_images:
        verify_required_images(plan.required_images)


def run_app_setup(install_paths: InstallPaths, integrations: List[str]) -> None:
    """Apply Django database setup in one-off managed application containers."""
    info("Applying Django database migrations and bootstrap data...")
    for django_args in (
        ["migrate", "--noinput"],
        ["populate_analytics"],
        ["create_periodic_tasks"],
    ):
        cmd = compose_runtime_cmd(install_paths, integrations) + [
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "web",
            "parseus",
            *django_args,
        ]
        r = run_streaming(
            cmd,
            timeout=LONG_CMD_TIMEOUT,
            heartbeat_message="Django database setup is still running",
        )
        if not r.ok:
            die(
                f"Failed to apply Django database setup:\nCommand: {' '.join(cmd)}\n\n"
                f"{r.stderr or r.stdout}"
            )

    ok("Django database setup complete")


def start_stack(
    install_paths: InstallPaths,
    integrations: List[str],
) -> None:
    run_app_setup(install_paths, integrations)
    bring_up_stack(install_paths, integrations)
    show_stack_status(install_paths, integrations)


def start_stack_and_wait(
    install_paths: InstallPaths,
    config: InstallerConfig,
    integrations: List[str],
    *,
    require_readiness: bool = True,
) -> None:
    start_stack(install_paths, integrations)
    wait_for_stack_readiness(config, require_readiness=require_readiness)


def wait_for_stack_readiness(
    config: InstallerConfig,
    *,
    require_readiness: bool = True,
) -> None:
    try:
        wait_for_port(config.health_host, config.https_port, timeout=60)
        wait_for_https_ready(
            config.public_url,
            timeout=90,
            allow_self_signed=not uses_external_tls_certificates(config),
        )
    except ManageParseusError as e:
        if require_readiness:
            raise
        warn(
            "Parseus was installed and the Docker stack was started, but the "
            "local readiness check did not complete successfully. This can happen "
            "when endpoint security, firewall, proxy, or certificate inspection "
            "software blocks the local probe. The application may still be "
            f"available in a browser at {config.public_url}. Readiness error: {e.message}"
        )
