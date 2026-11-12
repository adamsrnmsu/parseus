from __future__ import annotations

import argparse
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from manage_parseus_lib import cli
from manage_parseus_lib.commands import runtime as runtime_command
from manage_parseus_lib.commands import status as status_command
from manage_parseus_lib.commands import upgrade as upgrade_command
from manage_parseus_lib.commands.uninstall import remove_command_wrapper, remove_system_paths
from manage_parseus_lib.config.loader import build_installer_config
from manage_parseus_lib.config.model import InstallerConfig
from manage_parseus_lib.docker.compose import (
    _normalize_compose_port_entry,
    _path_writable_by_uid_gid,
    build_compose_profile_args,
    compose_base_cmd,
    compute_named_volumes,
    compute_required_images,
    verify_required_bind_mounts,
)
from manage_parseus_lib.errors import ManageParseusError
from manage_parseus_lib.install.certificates import copy_tls_file_if_changed
from manage_parseus_lib.install.files import (
    build_compose_env,
    copy_required_files,
    install_command_wrapper,
    read_project_version,
    wrapper_points_to_target,
)
from manage_parseus_lib.install.env_files import (
    apply_env_overrides_to_specs,
    django_host_and_origin_defaults,
    ensure_managed_env_file,
    encode_env_value,
    ensure_compose_env_file,
    get_env_file_specs,
    parse_env_file,
    write_env_file,
)
from manage_parseus_lib.install.paths import (
    InstallPaths,
    build_paths,
    has_complete_install_state,
    has_runtime_install_markers,
    is_installed,
    require_source_outside_install_root,
    source_is_install_root,
)
from manage_parseus_lib.integrations import parse_integrations_value
from manage_parseus_lib.output import die
from manage_parseus_lib.docker.images import build_docker_images, pull_docker_images
from manage_parseus_lib.docker.volumes import filter_volumes_by_suffix
from manage_parseus_lib.state.manifest import (
    app_version_from_image_ref,
    build_install_manifest,
    build_manifest_installer_config,
    manifest_named_volumes_value,
    manifests_differ_meaningfully,
    resolve_app_version,
    stable_config_fingerprint,
    validate_manifest_schema,
)
from manage_parseus_lib.system import accounts as system_accounts
from manage_parseus_lib.system import permissions as system_permissions
from manage_parseus_lib.system import preflight as system_preflight
from manage_parseus_lib.system import systemd as system_systemd
from manage_parseus_lib.process import CmdResult
from manage_parseus_lib.workflows.integrations import (
    resolve_existing_install_integrations,
    resolve_install_integrations,
    resolve_status_integrations,
)
from manage_parseus_lib.workflows.stack import (
    StackPlan,
    build_stack_plan,
    run_app_setup,
    start_stack_and_wait,
    verify_stack_plan,
)


def test_die_raises_manage_parseus_error() -> None:
    with pytest.raises(ManageParseusError) as exc_info:
        die("bad thing", rc=7)

    assert exc_info.value.message == "bad thing"
    assert exc_info.value.rc == 7


def test_cli_main_converts_manage_parseus_error_to_exit_code(monkeypatch, capsys) -> None:
    def fake_run_cli(argv):
        raise ManageParseusError("configuration failed", rc=3)

    monkeypatch.setattr(cli, "run_cli", fake_run_cli)

    assert cli.main(["status"]) == 3
    captured = capsys.readouterr()
    assert "configuration failed" in captured.err


def test_require_root_for_system_install_rejects_non_root(monkeypatch) -> None:
    monkeypatch.setattr(system_preflight.os, "geteuid", lambda: 1000)

    with pytest.raises(ManageParseusError) as exc_info:
        system_preflight.require_root_for_system_install("install")

    assert "requires root privileges" in exc_info.value.message
    assert "sudo manage-parseus install" in exc_info.value.message


def test_require_root_for_system_install_allows_root(monkeypatch) -> None:
    monkeypatch.setattr(system_preflight.os, "geteuid", lambda: 0)

    system_preflight.require_root_for_system_install("install")


def test_require_admin_privileges_allows_root(monkeypatch) -> None:
    monkeypatch.setattr(system_preflight.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        system_preflight,
        "process_in_group",
        lambda group: pytest.fail("should not check group membership for root"),
    )

    system_preflight.require_admin_privileges("status")


def test_require_admin_privileges_allows_admin_group_member(monkeypatch) -> None:
    monkeypatch.setattr(system_preflight.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        system_preflight, "process_in_group", lambda group: group == "parseus-admin"
    )

    system_preflight.require_admin_privileges("status")


def test_require_admin_privileges_rejects_non_admin_non_root(monkeypatch) -> None:
    monkeypatch.setattr(system_preflight.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(system_preflight, "process_in_group", lambda group: False)
    monkeypatch.setattr(system_preflight, "user_listed_in_group", lambda group: False)

    with pytest.raises(ManageParseusError) as exc_info:
        system_preflight.require_admin_privileges("status")

    assert "parseus-admin" in exc_info.value.message
    assert "sudo manage-parseus status" in exc_info.value.message


def test_require_admin_privileges_rejects_stale_session_member(monkeypatch) -> None:
    # In /etc/group but not active in the running session's group set.
    monkeypatch.setattr(system_preflight.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(system_preflight, "process_in_group", lambda group: False)
    monkeypatch.setattr(
        system_preflight, "user_listed_in_group", lambda group: group == "parseus-admin"
    )

    with pytest.raises(ManageParseusError) as exc_info:
        system_preflight.require_admin_privileges("status")

    message = exc_info.value.message
    assert "does not have it active yet" in message
    assert "newgrp parseus-admin" in message


def test_find_docker_reports_permission_denied_clearly(monkeypatch) -> None:
    monkeypatch.setattr(system_preflight.shutil, "which", lambda name: "/usr/bin/docker")

    def fake_run(cmd, timeout=None):
        if cmd[:2] == ["docker", "--version"]:
            return CmdResult(ok=True, stdout="Docker version 27.0.0")
        if cmd[:2] == ["docker", "info"]:
            return CmdResult(
                ok=False,
                stdout="",
                stderr="Got permission denied while trying to connect to the "
                "Docker daemon socket at unix:///var/run/docker.sock",
            )
        return CmdResult(ok=True, stdout="")

    monkeypatch.setattr(system_preflight, "run", fake_run)

    with pytest.raises(ManageParseusError) as exc_info:
        system_preflight.find_docker()

    message = exc_info.value.message
    assert "cannot access it" in message
    assert "sudo manage-parseus" in message
    assert "docker` group" in message


def test_find_docker_reports_daemon_down_clearly(monkeypatch) -> None:
    monkeypatch.setattr(system_preflight.shutil, "which", lambda name: "/usr/bin/docker")

    def fake_run(cmd, timeout=None):
        if cmd[:2] == ["docker", "--version"]:
            return CmdResult(ok=True, stdout="Docker version 27.0.0")
        if cmd[:2] == ["docker", "info"]:
            return CmdResult(
                ok=False,
                stdout="",
                stderr="Cannot connect to the Docker daemon at "
                "unix:///var/run/docker.sock. Is the docker daemon running?",
            )
        return CmdResult(ok=True, stdout="")

    monkeypatch.setattr(system_preflight, "run", fake_run)

    with pytest.raises(ManageParseusError) as exc_info:
        system_preflight.find_docker()

    message = exc_info.value.message
    assert "not reachable" in message
    assert "systemctl start docker" in message


def test_runtime_commands_require_admin_not_full_root(monkeypatch, tmp_path: Path) -> None:
    paths = _test_install_paths(tmp_path)
    paths.app_root.mkdir(parents=True)
    paths.config_root.mkdir(parents=True)
    paths.runtime_root.mkdir(parents=True)
    paths.state_dir.mkdir(parents=True)
    paths.runtime_compose.write_text("services: {}\n", encoding="utf-8")
    paths.images_compose.write_text("services: {}\n", encoding="utf-8")
    paths.current_manifest.write_text('{"schema_version": 1}\n', encoding="utf-8")

    # Neither root nor a parseus-admin group member.
    monkeypatch.setattr(system_preflight.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(system_preflight, "process_in_group", lambda group: False)
    monkeypatch.setattr(system_preflight, "user_listed_in_group", lambda group: False)
    args = SimpleNamespace(
        deployment_context=SimpleNamespace(
            config=InstallerConfig(),
            paths=paths,
        )
    )

    with pytest.raises(ManageParseusError, match="parseus-admin"):
        runtime_command.do_down(args)
    with pytest.raises(ManageParseusError, match="parseus-admin"):
        status_command.do_status(args)


def _run_do_up(monkeypatch, tmp_path: Path, *, manifest, requested, stop_ok=True):
    """Run do_up with its collaborators faked; return (events, start_kwargs)."""
    events: list = []
    started: dict = {}

    monkeypatch.setattr(runtime_command, "require_admin_privileges", lambda name: None)
    monkeypatch.setattr(runtime_command, "require_installed", lambda paths: None)
    monkeypatch.setattr(runtime_command, "preflight", lambda **kwargs: None)
    monkeypatch.setattr(runtime_command, "load_manifest", lambda path: manifest)

    def fake_stop() -> bool:
        events.append("stop_via_systemd")
        return stop_ok

    def fake_down(paths, integrations) -> None:
        events.append(("compose_down", list(integrations)))

    def fake_start(paths, config, integrations, old_manifest, open_browser, message, **kwargs):
        events.append("start")
        started["integrations"] = integrations
        started["message"] = message
        started["kwargs"] = kwargs

    monkeypatch.setattr(runtime_command, "stop_via_systemd", fake_stop)
    monkeypatch.setattr(runtime_command, "bring_down_stack_if_possible", fake_down)
    monkeypatch.setattr(runtime_command, "start_stack_and_maybe_update_manifest", fake_start)

    args = SimpleNamespace(
        deployment_context=SimpleNamespace(
            config=InstallerConfig(), paths=_test_install_paths(tmp_path)
        ),
        integrations=requested,
        integrations_source="--integrations",
        open_browser=False,
    )
    runtime_command.do_up(args)
    return events, started


def test_up_with_changed_integrations_does_full_restart(monkeypatch, tmp_path: Path) -> None:
    events, started = _run_do_up(
        monkeypatch,
        tmp_path,
        manifest={"schema_version": 1, "integrations": ["gpib"]},
        requested="winlog",
    )

    assert events == ["stop_via_systemd", "start"]
    assert started["integrations"] == ["winlog"]
    assert "restarted" in started["message"]
    assert started["kwargs"]["skip_port_check_when_running"] is False


def test_up_with_changed_integrations_falls_back_to_compose_down(
    monkeypatch, tmp_path: Path
) -> None:
    events, _ = _run_do_up(
        monkeypatch,
        tmp_path,
        manifest={"schema_version": 1, "integrations": ["gpib"]},
        requested="winlog",
        stop_ok=False,
    )

    # The stack is brought down using the PREVIOUS integrations so their
    # profile services are stopped too.
    assert events == ["stop_via_systemd", ("compose_down", ["gpib"]), "start"]


def test_up_with_unchanged_integrations_does_not_restart(monkeypatch, tmp_path: Path) -> None:
    events, started = _run_do_up(
        monkeypatch,
        tmp_path,
        manifest={"schema_version": 1, "integrations": ["gpib"]},
        requested="gpib",
    )

    assert events == ["start"]
    assert started["message"].startswith("Parseus started.")
    assert started["kwargs"]["skip_port_check_when_running"] is True


def test_up_without_integrations_flag_reuses_manifest_and_does_not_restart(
    monkeypatch, tmp_path: Path
) -> None:
    events, started = _run_do_up(
        monkeypatch,
        tmp_path,
        manifest={"schema_version": 1, "integrations": ["gpib"]},
        requested=None,
    )

    assert events == ["start"]
    assert started["integrations"] == ["gpib"]


def test_up_without_manifest_does_not_restart(monkeypatch, tmp_path: Path) -> None:
    events, started = _run_do_up(
        monkeypatch, tmp_path, manifest=None, requested="gpib"
    )

    assert events == ["start"]
    assert started["kwargs"]["skip_port_check_when_running"] is True


def test_up_integration_order_change_is_not_a_change(monkeypatch, tmp_path: Path) -> None:
    events, _ = _run_do_up(
        monkeypatch,
        tmp_path,
        manifest={"schema_version": 1, "integrations": ["gpib", "winlog"]},
        requested="winlog,gpib",
    )

    assert events == ["start"]


def test_status_requires_admin_but_not_full_root(monkeypatch, tmp_path: Path, capsys) -> None:
    paths = _test_install_paths(tmp_path)
    paths.app_root.mkdir(parents=True)
    paths.config_root.mkdir(parents=True)
    paths.runtime_root.mkdir(parents=True)
    paths.state_dir.mkdir(parents=True)
    paths.runtime_compose.write_text("services: {}\n", encoding="utf-8")
    paths.images_compose.write_text("services: {}\n", encoding="utf-8")
    paths.current_manifest.write_text(
        '{"schema_version": 1, "app_version": "1.2.3"}\n',
        encoding="utf-8",
    )

    # A parseus-admin group member who is not root.
    monkeypatch.setattr(system_preflight.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        system_preflight, "process_in_group", lambda group: group == "parseus-admin"
    )
    monkeypatch.setattr(status_command, "port_is_available", lambda host, port: True)
    monkeypatch.setattr(
        status_command,
        "show_compose_status_optional",
        lambda install_paths, integrations: None,
    )

    args = SimpleNamespace(
        deployment_context=SimpleNamespace(config=InstallerConfig(), paths=paths),
        integrations=None,
        integrations_source="--integrations",
    )

    status_command.do_status(args)

    out = capsys.readouterr().out
    assert "Installed: yes" in out
    assert "App version: 1.2.3" in out


def test_upgrade_parser_accepts_allow_downgrade() -> None:
    args = cli.get_args(["upgrade", "--allow-downgrade"])

    assert args.allow_downgrade is True


def test_upgrade_version_policy_rejects_downgrade_by_default() -> None:
    with pytest.raises(ManageParseusError, match="Refusing to downgrade"):
        upgrade_command.enforce_upgrade_version_policy(
            {"app_version": "2.0.0"},
            {"app_version": "1.9.0"},
            allow_downgrade=False,
        )


def test_upgrade_version_policy_allows_explicit_downgrade(capsys) -> None:
    upgrade_command.enforce_upgrade_version_policy(
        {"app_version": "2.0.0"},
        {"app_version": "1.9.0"},
        allow_downgrade=True,
    )

    assert "Upgrading Parseus 2.0.0 -> 1.9.0" in capsys.readouterr().out


def test_upgrade_version_policy_tolerates_unknown_versions(capsys) -> None:
    upgrade_command.enforce_upgrade_version_policy(
        {"app_version": "unknown"},
        {"app_version": "dev"},
        allow_downgrade=False,
    )

    captured = capsys.readouterr()
    assert "Upgrading Parseus unknown -> dev" in captured.out
    assert "Could not compare Parseus versions" in captured.err


def _paths_for(source_root: Path, app_root: Path) -> InstallPaths:
    return InstallPaths(source_root=source_root, app_root=app_root)


def test_source_is_install_root_true_for_same_directory(tmp_path: Path) -> None:
    app_root = tmp_path / "opt-parseus"
    app_root.mkdir()

    assert source_is_install_root(_paths_for(app_root, app_root)) is True


def test_source_is_install_root_true_for_symlink_to_app_root(tmp_path: Path) -> None:
    app_root = tmp_path / "opt-parseus"
    app_root.mkdir()
    link = tmp_path / "link"
    link.symlink_to(app_root, target_is_directory=True)

    assert source_is_install_root(_paths_for(link, app_root)) is True


def test_source_is_install_root_true_for_source_nested_in_app_root(tmp_path: Path) -> None:
    app_root = tmp_path / "opt-parseus"
    nested = app_root / "releases" / "1.2.4"
    nested.mkdir(parents=True)

    assert source_is_install_root(_paths_for(nested, app_root)) is True


def test_source_is_install_root_false_for_separate_directory(tmp_path: Path) -> None:
    app_root = tmp_path / "opt-parseus"
    source = tmp_path / "parseus-1.2.4"
    app_root.mkdir()
    source.mkdir()

    assert source_is_install_root(_paths_for(source, app_root)) is False


def test_source_is_install_root_false_for_sibling_with_shared_name_prefix(tmp_path: Path) -> None:
    app_root = tmp_path / "parseus"
    source = tmp_path / "parseus-new"
    app_root.mkdir()
    source.mkdir()

    assert source_is_install_root(_paths_for(source, app_root)) is False


def test_source_is_install_root_false_when_app_root_missing(tmp_path: Path) -> None:
    source = tmp_path / "parseus-1.2.4"
    source.mkdir()

    assert source_is_install_root(_paths_for(source, tmp_path / "missing")) is False


def test_require_source_outside_install_root_rejects_installed_copy(tmp_path: Path) -> None:
    app_root = tmp_path / "opt-parseus"
    app_root.mkdir()

    with pytest.raises(ManageParseusError, match="Refusing to upgrade from the installed copy"):
        require_source_outside_install_root(_paths_for(app_root, app_root), "upgrade")


def test_require_source_outside_install_root_allows_release_bundle(tmp_path: Path) -> None:
    app_root = tmp_path / "opt-parseus"
    source = tmp_path / "parseus-1.2.4"
    app_root.mkdir()
    source.mkdir()

    require_source_outside_install_root(_paths_for(source, app_root), "upgrade")


def _fake_systemctl(monkeypatch, *, active: bool, calls: list, fail: bool = False):
    def fake(args):
        calls.append(args)
        if args[0] == "is-active":
            return CmdResult(ok=active, stdout="active" if active else "inactive")
        return CmdResult(ok=not fail, stderr="boom" if fail else "")

    monkeypatch.setattr(system_systemd, "systemd_available", lambda: True)
    monkeypatch.setattr(system_systemd, "_systemctl", fake)


def test_start_via_systemd_starts_inactive_unit(monkeypatch) -> None:
    calls: list = []
    _fake_systemctl(monkeypatch, active=False, calls=calls)

    assert system_systemd.start_via_systemd() is True
    assert ["start", system_systemd.SYSTEMD_UNIT_NAME] in calls
    assert ["reload", system_systemd.SYSTEMD_UNIT_NAME] not in calls


def test_start_via_systemd_reloads_already_active_oneshot_unit(monkeypatch) -> None:
    calls: list = []
    _fake_systemctl(monkeypatch, active=True, calls=calls)

    assert system_systemd.start_via_systemd() is True
    assert ["reload", system_systemd.SYSTEMD_UNIT_NAME] in calls
    assert ["start", system_systemd.SYSTEMD_UNIT_NAME] not in calls


def test_start_via_systemd_reports_failure_of_reload(monkeypatch, capsys) -> None:
    calls: list = []
    _fake_systemctl(monkeypatch, active=True, calls=calls, fail=True)

    assert system_systemd.start_via_systemd() is False
    assert "systemctl reload failed" in capsys.readouterr().err


def test_start_via_systemd_false_without_systemd(monkeypatch) -> None:
    monkeypatch.setattr(system_systemd, "systemd_available", lambda: False)

    assert system_systemd.start_via_systemd() is False


def test_ensure_system_accounts_creates_missing_identities(monkeypatch) -> None:
    commands = []

    monkeypatch.setattr(system_accounts, "group_exists", lambda name: False)
    monkeypatch.setattr(system_accounts, "user_exists", lambda name: False)
    monkeypatch.setattr(system_accounts, "require_command", lambda name: None)

    def fake_run(cmd, timeout=None):
        commands.append(cmd)
        return CmdResult(ok=True, stdout="created")

    monkeypatch.setattr(system_accounts, "run", fake_run)

    result = system_accounts.ensure_system_accounts()

    assert result.created_service_group is True
    assert result.created_admin_group is True
    assert result.created_service_user is True
    assert commands == [
        ["groupadd", "--system", "parseus"],
        ["groupadd", "--system", "parseus-admin"],
        [
            "useradd",
            "--system",
            "--gid",
            "parseus",
            "--home-dir",
            "/var/lib/parseus",
            "--shell",
            "/usr/sbin/nologin",
            "--comment",
            "Parseus service user",
            "parseus",
        ],
    ]


def test_ensure_system_accounts_reuses_valid_existing_identities(monkeypatch) -> None:
    monkeypatch.setattr(system_accounts, "group_exists", lambda name: True)
    monkeypatch.setattr(system_accounts, "user_exists", lambda name: True)
    monkeypatch.setattr(system_accounts, "validate_existing_service_user", lambda user, group: None)
    monkeypatch.setattr(
        system_accounts,
        "run",
        lambda cmd, timeout=None: pytest.fail("existing accounts should not be created"),
    )

    result = system_accounts.ensure_system_accounts()

    assert result.created_service_group is False
    assert result.created_admin_group is False
    assert result.created_service_user is False


def test_validate_existing_service_user_rejects_login_shell(monkeypatch) -> None:
    monkeypatch.setattr(
        system_accounts.pwd,
        "getpwnam",
        lambda user: SimpleNamespace(pw_gid=123, pw_dir="/var/lib/parseus", pw_shell="/bin/bash"),
    )
    monkeypatch.setattr(
        system_accounts.grp,
        "getgrgid",
        lambda gid: SimpleNamespace(gr_name="parseus"),
    )

    with pytest.raises(ManageParseusError, match="login shell"):
        system_accounts.validate_existing_service_user("parseus", "parseus")


def test_system_account_provisioning_serializes_manifest_data() -> None:
    provisioning = system_accounts.SystemAccountProvisioning(
        created_service_user=True,
        created_service_group=True,
        created_admin_group=False,
    )

    assert provisioning.as_manifest_data() == {
        "service_user": "parseus",
        "service_group": "parseus",
        "admin_group": "parseus-admin",
        "created_service_user": True,
        "created_service_group": True,
        "created_admin_group": False,
    }


def test_remove_system_accounts_uses_manifest_created_flags(monkeypatch) -> None:
    calls = []
    manifest = {
        "system_accounts": {
            "service_user": "parseus",
            "service_group": "parseus",
            "admin_group": "parseus-admin",
            "created_service_user": True,
            "created_service_group": False,
            "created_admin_group": True,
        }
    }

    monkeypatch.setattr(system_accounts, "remove_system_user", lambda name: calls.append(("user", name)))
    monkeypatch.setattr(system_accounts, "remove_system_group", lambda name: calls.append(("group", name)))

    system_accounts.remove_system_accounts(manifest)

    assert calls == [("user", "parseus"), ("group", "parseus-admin")]


def test_build_paths_uses_var_lib_for_manifest_state() -> None:
    paths = build_paths()

    # build_paths() resolves symlinks, so /var -> /private/var on macOS.
    state = Path("/var/lib/parseus/state").resolve()
    assert paths.state_dir == state
    assert paths.current_manifest == state / "current_manifest.json"
    assert paths.history_dir == state / "history"


def _test_install_paths(tmp_path: Path):
    paths = build_paths(source_root=tmp_path / "source")
    return type(paths)(
        source_root=paths.source_root,
        app_root=tmp_path / "opt" / "parseus",
        config_root=tmp_path / "etc" / "parseus",
        data_root=tmp_path / "var" / "lib" / "parseus",
        state_root=tmp_path / "var" / "lib" / "parseus" / "state",
        runtime_root=tmp_path / "var" / "lib" / "parseus" / "runtime",
        drop_dir=tmp_path / "srv" / "parseus" / "ftp",
        log_root=tmp_path / "var" / "log" / "parseus",
        wrapper_path=tmp_path / "usr" / "local" / "bin" / "manage-parseus",
        project_name=paths.project_name,
    )


def test_install_markers_do_not_require_manifest(tmp_path: Path) -> None:
    paths = _test_install_paths(tmp_path)

    assert is_installed(paths) is False
    assert has_runtime_install_markers(paths) is False
    assert has_complete_install_state(paths) is False

    paths.app_root.mkdir(parents=True)
    paths.config_root.mkdir(parents=True)
    paths.runtime_root.mkdir(parents=True)
    paths.runtime_compose.write_text("services: {}\n", encoding="utf-8")
    paths.images_compose.write_text("services: {}\n", encoding="utf-8")

    assert is_installed(paths) is True
    assert has_runtime_install_markers(paths) is True
    assert has_complete_install_state(paths) is False

    paths.state_dir.mkdir(parents=True)
    paths.current_manifest.write_text('{"schema_version": 1}\n', encoding="utf-8")

    assert has_complete_install_state(paths) is True


def test_status_reports_installed_with_missing_manifest(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    paths = _test_install_paths(tmp_path)
    paths.app_root.mkdir(parents=True)
    paths.config_root.mkdir(parents=True)
    paths.runtime_root.mkdir(parents=True)
    paths.runtime_compose.write_text("services: {}\n", encoding="utf-8")
    paths.images_compose.write_text("services: {}\n", encoding="utf-8")

    monkeypatch.setattr(
        status_command,
        "require_admin_privileges",
        lambda command: None,
    )
    monkeypatch.setattr(
        status_command,
        "port_is_available",
        lambda host, port: True,
    )
    monkeypatch.setattr(
        status_command,
        "show_compose_status_optional",
        lambda install_paths, integrations: None,
    )

    args = SimpleNamespace(
        deployment_context=SimpleNamespace(
            config=InstallerConfig(),
            paths=paths,
        ),
        integrations=None,
        integrations_source="--integrations",
    )

    status_command.do_status(args)

    out = capsys.readouterr().out
    assert "Installed: yes" in out
    assert "Installation state: runtime markers present, manifest missing" in out
    assert "Manifest: not found" in out


def test_status_manifest_permission_error_is_friendly(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths = _test_install_paths(tmp_path)
    paths.state_dir.mkdir(parents=True)
    paths.current_manifest.write_text('{"schema_version": 1}\n', encoding="utf-8")

    def _boom(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(status_command, "load_manifest", _boom)

    with pytest.raises(ManageParseusError) as exc_info:
        status_command._load_status_manifest(paths)

    message = exc_info.value.message
    assert "Cannot read the Parseus install manifest" in message
    assert "newgrp parseus-admin" in message


def test_build_directory_specs_uses_system_layout(monkeypatch, tmp_path: Path) -> None:
    paths = SimpleNamespace(
        app_root=tmp_path / "opt" / "parseus",
        config_root=tmp_path / "etc" / "parseus",
        env_dir=tmp_path / "etc" / "parseus" / "envs",
        certs_dir=tmp_path / "etc" / "parseus" / "certs",
        data_root=tmp_path / "var" / "lib" / "parseus",
        state_dir=tmp_path / "var" / "lib" / "parseus" / "state",
        history_dir=tmp_path / "var" / "lib" / "parseus" / "state" / "history",
        runtime_root=tmp_path / "var" / "lib" / "parseus" / "runtime",
        winlog_logstash_state_dir=tmp_path / "var" / "lib" / "parseus" / "winlog" / "logstash",
        drop_dir=tmp_path / "srv" / "parseus" / "ftp",
        log_root=tmp_path / "var" / "log" / "parseus",
    )
    monkeypatch.setattr(system_permissions, "user_id", lambda name: 2000)
    monkeypatch.setattr(
        system_permissions,
        "group_id",
        lambda name: 3000 if name == "parseus" else 3001,
    )

    specs = system_permissions.build_directory_specs(paths)
    by_path = {spec.path: spec for spec in specs}

    assert by_path[paths.app_root].owner_uid == 0
    assert by_path[paths.app_root].owner_gid == 0
    assert by_path[paths.app_root].mode == 0o755
    assert by_path[paths.config_root].owner_uid == 0
    assert by_path[paths.config_root].owner_gid == 3001
    assert by_path[paths.config_root].mode == 0o750
    assert by_path[paths.state_dir].owner_uid == 2000
    assert by_path[paths.state_dir].owner_gid == 3001
    assert by_path[paths.state_dir].mode == 0o750
    assert by_path[paths.drop_dir].owner_uid == 1000
    assert by_path[paths.drop_dir].owner_gid == 1000
    assert by_path[paths.drop_dir].mode == 0o750


def test_ensure_directory_spec_creates_chowns_chmods_and_verifies(monkeypatch, tmp_path: Path) -> None:
    calls = []
    spec = system_permissions.DirectorySpec(
        tmp_path / "parseus",
        mode=0o750,
        owner_uid=100,
        owner_gid=200,
        label="test directory",
    )

    monkeypatch.setattr(
        system_permissions.os,
        "chown",
        lambda path, uid, gid: calls.append(("chown", path, uid, gid)),
    )
    monkeypatch.setattr(
        system_permissions.os,
        "chmod",
        lambda path, mode: calls.append(("chmod", path, mode)),
    )
    monkeypatch.setattr(
        system_permissions,
        "verify_directory_spec",
        lambda verified: calls.append(("verify", verified)),
    )

    system_permissions.ensure_directory_spec(spec)

    assert spec.path.is_dir()
    assert calls == [
        ("chown", spec.path, 100, 200),
        ("chmod", spec.path, 0o750),
        ("verify", spec),
    ]


def test_verify_directory_spec_rejects_files(tmp_path: Path) -> None:
    path = tmp_path / "not-a-directory"
    path.write_text("not a dir", encoding="utf-8")
    spec = system_permissions.DirectorySpec(
        path,
        mode=0o750,
        owner_uid=0,
        owner_gid=0,
        label="test directory",
    )

    with pytest.raises(ManageParseusError, match="not a directory"):
        system_permissions.verify_directory_spec(spec)


def test_set_config_file_permissions_uses_admin_group(monkeypatch, tmp_path: Path) -> None:
    calls = []
    path = tmp_path / "production.env"
    path.write_text("KEY=value\n", encoding="utf-8")

    monkeypatch.setattr(system_permissions, "group_id", lambda name: 4321)
    monkeypatch.setattr(
        system_permissions.os,
        "chown",
        lambda target, uid, gid: calls.append(("chown", target, uid, gid)),
    )
    monkeypatch.setattr(
        system_permissions.os,
        "chmod",
        lambda target, mode: calls.append(("chmod", target, mode)),
    )

    system_permissions.set_config_file_permissions(path)

    assert calls == [
        ("chown", path, 0, 4321),
        ("chmod", path, 0o640),
    ]


def test_write_env_file_applies_config_permissions_as_root(monkeypatch, tmp_path: Path) -> None:
    calls = []
    env_path = tmp_path / "compose.env"

    monkeypatch.setattr("manage_parseus_lib.install.env_files.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "manage_parseus_lib.install.env_files.set_config_file_permissions",
        lambda path, mode: calls.append((path, mode)),
    )

    write_env_file(env_path, {"PARSEUS_PROJECT_NAME": "parseus"})

    assert calls == [(env_path, 0o640)]
    assert env_path.stat().st_mode & 0o777 == 0o640


def test_ensure_compose_env_file_skips_write_when_unchanged(monkeypatch, tmp_path: Path) -> None:
    paths = _test_install_paths(tmp_path)
    paths.compose_env_file.parent.mkdir(parents=True)
    paths.compose_env_file.write_text('PARSEUS_VERSION="1.2.3"\n', encoding="utf-8")

    def fail_write(*args, **kwargs):
        raise AssertionError("compose.env must not be rewritten when unchanged")

    monkeypatch.setattr("manage_parseus_lib.install.env_files.write_env_file", fail_write)

    ensure_compose_env_file(paths, {"PARSEUS_VERSION": "1.2.3"})


def test_ensure_compose_env_file_writes_when_changed(monkeypatch, tmp_path: Path) -> None:
    paths = _test_install_paths(tmp_path)
    paths.compose_env_file.parent.mkdir(parents=True)
    paths.compose_env_file.write_text('PARSEUS_VERSION="1.2.3"\n', encoding="utf-8")
    monkeypatch.setattr("manage_parseus_lib.install.env_files.os.geteuid", lambda: 1000)

    ensure_compose_env_file(paths, {"PARSEUS_VERSION": "1.2.4"})

    assert 'PARSEUS_VERSION="1.2.4"' in paths.compose_env_file.read_text(encoding="utf-8")


def test_ensure_compose_env_file_writes_when_missing(monkeypatch, tmp_path: Path) -> None:
    paths = _test_install_paths(tmp_path)
    monkeypatch.setattr("manage_parseus_lib.install.env_files.os.geteuid", lambda: 1000)

    ensure_compose_env_file(paths, {"PARSEUS_VERSION": "1.2.3"})

    assert paths.compose_env_file.exists()


def test_write_env_file_permission_error_is_a_clear_message(monkeypatch, tmp_path: Path) -> None:
    def deny(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("manage_parseus_lib.install.env_files.atomic_write_text", deny)

    with pytest.raises(ManageParseusError, match="requires root privileges") as exc_info:
        write_env_file(tmp_path / "compose.env", {"A": "b"})

    assert "sudo manage-parseus" in exc_info.value.message


def test_write_systemd_integration_env_skips_unchanged_file(monkeypatch, tmp_path: Path) -> None:
    paths = _test_install_paths(tmp_path)
    env_path = paths.runtime_root / system_systemd.SYSTEMD_ENV_FILENAME
    env_path.parent.mkdir(parents=True)
    env_path.write_text(system_systemd._render_env_file([]), encoding="utf-8")
    monkeypatch.setattr(system_systemd, "systemd_available", lambda: True)

    def fail_write(*args, **kwargs):
        raise AssertionError("env file must not be rewritten when unchanged")

    monkeypatch.setattr(system_systemd, "atomic_write_text", fail_write)

    system_systemd.write_systemd_integration_env(paths, [])


def test_write_systemd_integration_env_writes_when_changed(monkeypatch, tmp_path: Path) -> None:
    paths = _test_install_paths(tmp_path)
    env_path = paths.runtime_root / system_systemd.SYSTEMD_ENV_FILENAME
    env_path.parent.mkdir(parents=True)
    env_path.write_text(system_systemd._render_env_file([]), encoding="utf-8")
    monkeypatch.setattr(system_systemd, "systemd_available", lambda: True)

    system_systemd.write_systemd_integration_env(paths, ["gpib"])

    assert env_path.read_text(encoding="utf-8") == system_systemd._render_env_file(["gpib"])


def test_write_systemd_integration_env_permission_error_is_a_clear_message(
    monkeypatch, tmp_path: Path
) -> None:
    paths = _test_install_paths(tmp_path)
    monkeypatch.setattr(system_systemd, "systemd_available", lambda: True)

    def deny(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(system_systemd, "atomic_write_text", deny)

    with pytest.raises(ManageParseusError, match="requires root privileges"):
        system_systemd.write_systemd_integration_env(paths, ["gpib"])


def test_copy_tls_file_sets_config_permissions(monkeypatch, tmp_path: Path) -> None:
    calls = []
    src = tmp_path / "source.key"
    dst = tmp_path / "nginx-selfsigned.key"
    src.write_bytes(b"key")

    monkeypatch.setattr(
        "manage_parseus_lib.install.certificates.set_config_file_permissions",
        lambda path, mode: calls.append((path, mode)),
    )

    copy_tls_file_if_changed(src, dst, mode=0o640, label="private key")

    assert dst.read_bytes() == b"key"
    assert dst.stat().st_mode & 0o777 == 0o640
    assert calls == [(dst, 0o640)]


def test_set_app_bundle_permissions_normalizes_modes(monkeypatch, tmp_path: Path) -> None:
    chown_calls = []
    app_root = tmp_path / "parseus"
    script = app_root / "manage_parseus.py"
    data = app_root / "README.md"
    nested = app_root / "docs"
    nested_file = nested / "guide.md"
    nested.mkdir(parents=True)
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    data.write_text("readme\n", encoding="utf-8")
    nested_file.write_text("guide\n", encoding="utf-8")
    script.chmod(0o777)
    data.chmod(0o666)
    nested_file.chmod(0o666)

    monkeypatch.setattr(
        system_permissions.os,
        "chown",
        lambda path, uid, gid: chown_calls.append((path, uid, gid)),
    )

    system_permissions.set_app_bundle_permissions(app_root)

    assert app_root.stat().st_mode & 0o777 == 0o755
    assert nested.stat().st_mode & 0o777 == 0o755
    assert script.stat().st_mode & 0o777 == 0o755
    assert data.stat().st_mode & 0o777 == 0o644
    assert nested_file.stat().st_mode & 0o777 == 0o644
    assert all(uid == 0 and gid == 0 for _, uid, gid in chown_calls)


def test_set_runtime_file_permissions_uses_service_group(monkeypatch, tmp_path: Path) -> None:
    calls = []
    path = tmp_path / "compose.runtime.yaml"
    path.write_text("services: {}\n", encoding="utf-8")

    monkeypatch.setattr(system_permissions, "group_id", lambda name: 2468)
    monkeypatch.setattr(
        system_permissions.os,
        "chown",
        lambda target, uid, gid: calls.append(("chown", target, uid, gid)),
    )
    monkeypatch.setattr(
        system_permissions.os,
        "chmod",
        lambda target, mode: calls.append(("chmod", target, mode)),
    )

    system_permissions.set_runtime_file_permissions(path)

    assert calls == [
        ("chown", path, 0, 2468),
        ("chmod", path, 0o644),
    ]


def test_copy_required_files_uses_app_root_and_sets_bundle_permissions(monkeypatch, tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    app_root = tmp_path / "opt" / "parseus"
    source_root.mkdir()
    for name in [
        "README.md",
        "pyproject.toml",
        "compose.runtime.yaml",
        "compose.images.yaml",
        "nginx.conf",
        "entrypoint.sh",
        "manage_parseus.py",
    ]:
        (source_root / name).write_text(name, encoding="utf-8")
    for name in ["docs", "deploy", "manage_parseus_lib"]:
        path = source_root / name
        path.mkdir()
        (path / "placeholder.txt").write_text(name, encoding="utf-8")

    runtime_root = tmp_path / "var" / "lib" / "parseus" / "runtime"
    runtime_compose = runtime_root / "compose.runtime.yaml"
    images_compose = runtime_root / "compose.images.yaml"
    wrapper_path = tmp_path / "usr" / "local" / "bin" / "manage-parseus"
    app_calls = []
    runtime_calls = []
    paths = SimpleNamespace(
        source_root=source_root,
        app_root=app_root,
        runtime_compose=runtime_compose,
        images_compose=images_compose,
        wrapper_path=wrapper_path,
    )
    monkeypatch.setattr(
        "manage_parseus_lib.install.files.set_app_bundle_permissions",
        lambda path: app_calls.append(path),
    )
    monkeypatch.setattr(
        "manage_parseus_lib.install.files.set_runtime_file_permissions",
        lambda path: runtime_calls.append(path),
    )

    copy_required_files(paths)

    assert (app_root / "manage_parseus.py").read_text(encoding="utf-8") == "manage_parseus.py"
    assert (app_root / "pyproject.toml").read_text(encoding="utf-8") == "pyproject.toml"
    assert (app_root / "docs" / "placeholder.txt").read_text(encoding="utf-8") == "docs"
    assert not (app_root / "compose.runtime.yaml").exists()
    assert runtime_compose.read_text(encoding="utf-8") == "compose.runtime.yaml"
    assert images_compose.read_text(encoding="utf-8") == "compose.images.yaml"
    assert app_root in app_calls
    assert app_root / "manage_parseus.py" in app_calls
    assert app_root / "docs" in app_calls
    assert runtime_calls == [runtime_compose, images_compose]
    assert (app_root / "manage_parseus.py").stat().st_mode & 0o777 == 0o755
    assert wrapper_path.is_symlink()
    assert Path(os.readlink(wrapper_path)) == app_root / "manage_parseus.py"


def test_install_command_wrapper_refuses_non_parseus_wrapper(tmp_path: Path) -> None:
    app_root = tmp_path / "opt" / "parseus"
    wrapper_path = tmp_path / "usr" / "local" / "bin" / "manage-parseus"
    app_root.mkdir(parents=True)
    wrapper_path.parent.mkdir(parents=True)
    (app_root / "manage_parseus.py").write_text("", encoding="utf-8")
    wrapper_path.write_text("custom wrapper", encoding="utf-8")

    with pytest.raises(ManageParseusError, match="Refusing to overwrite"):
        install_command_wrapper(SimpleNamespace(app_root=app_root, wrapper_path=wrapper_path))


def test_wrapper_points_to_target_accepts_relative_symlink(tmp_path: Path) -> None:
    app_root = tmp_path / "opt" / "parseus"
    wrapper_path = app_root / "bin" / "manage-parseus"
    target = app_root / "manage_parseus.py"
    wrapper_path.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    wrapper_path.symlink_to(Path("..") / "manage_parseus.py")

    assert wrapper_points_to_target(wrapper_path, target) is True


def test_remove_command_wrapper_removes_only_parseus_symlink(tmp_path: Path) -> None:
    app_root = tmp_path / "opt" / "parseus"
    wrapper_path = tmp_path / "usr" / "local" / "bin" / "manage-parseus"
    app_root.mkdir(parents=True)
    wrapper_path.parent.mkdir(parents=True)
    target = app_root / "manage_parseus.py"
    target.write_text("", encoding="utf-8")
    wrapper_path.symlink_to(target)

    remove_command_wrapper(SimpleNamespace(app_root=app_root, wrapper_path=wrapper_path))

    assert not wrapper_path.exists()
    assert not wrapper_path.is_symlink()


def test_remove_command_wrapper_removes_relative_parseus_symlink(tmp_path: Path) -> None:
    app_root = tmp_path / "opt" / "parseus"
    wrapper_path = app_root / "bin" / "manage-parseus"
    app_root.mkdir(parents=True)
    wrapper_path.parent.mkdir(parents=True)
    target = app_root / "manage_parseus.py"
    target.write_text("", encoding="utf-8")
    wrapper_path.symlink_to(Path("..") / "manage_parseus.py")

    remove_command_wrapper(SimpleNamespace(app_root=app_root, wrapper_path=wrapper_path))

    assert not wrapper_path.exists()
    assert not wrapper_path.is_symlink()


def test_remove_command_wrapper_keeps_non_parseus_symlink(tmp_path: Path) -> None:
    app_root = tmp_path / "opt" / "parseus"
    wrapper_path = tmp_path / "usr" / "local" / "bin" / "manage-parseus"
    other_target = tmp_path / "other"
    app_root.mkdir(parents=True)
    wrapper_path.parent.mkdir(parents=True)
    other_target.write_text("", encoding="utf-8")
    wrapper_path.symlink_to(other_target)

    remove_command_wrapper(SimpleNamespace(app_root=app_root, wrapper_path=wrapper_path))

    assert wrapper_path.is_symlink()
    assert Path(os.readlink(wrapper_path)) == other_target


def test_remove_command_wrapper_does_not_touch_target_when_wrapper_path_misconfigured(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "opt" / "parseus"
    app_root.mkdir(parents=True)
    target = app_root / "manage_parseus.py"
    target.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    # Misconfigured: wrapper path resolves to the installed script itself.
    remove_command_wrapper(SimpleNamespace(app_root=app_root, wrapper_path=target))

    # The real script must survive and must not be unlinked.
    assert target.exists()
    assert not target.is_symlink()


def test_remove_system_paths_removes_managed_roots_and_keeps_drop_by_default(tmp_path: Path) -> None:
    paths = SimpleNamespace(
        app_root=tmp_path / "opt" / "parseus",
        config_root=tmp_path / "etc" / "parseus",
        data_root=tmp_path / "var" / "lib" / "parseus",
        log_root=tmp_path / "var" / "log" / "parseus",
        drop_dir=tmp_path / "srv" / "parseus" / "ftp",
        wrapper_path=tmp_path / "usr" / "local" / "bin" / "manage-parseus",
    )
    paths.env_dir = paths.config_root / "envs"
    paths.certs_dir = paths.config_root / "certs"
    paths.compose_env_file = paths.config_root / "compose.env"
    paths.current_manifest = paths.data_root / "state" / "current_manifest.json"
    paths.runtime_compose = paths.data_root / "runtime" / "compose.runtime.yaml"
    paths.images_compose = paths.data_root / "runtime" / "compose.images.yaml"

    (paths.app_root / "deploy").mkdir(parents=True)
    (paths.app_root / "manage_parseus.py").write_text("", encoding="utf-8")
    paths.env_dir.mkdir(parents=True)
    paths.certs_dir.mkdir(parents=True)
    paths.compose_env_file.write_text("", encoding="utf-8")
    paths.current_manifest.parent.mkdir(parents=True)
    paths.current_manifest.write_text("{}", encoding="utf-8")
    paths.runtime_compose.parent.mkdir(parents=True)
    paths.runtime_compose.write_text("services: {}", encoding="utf-8")
    paths.images_compose.write_text("services: {}", encoding="utf-8")
    paths.log_root.mkdir(parents=True)
    paths.drop_dir.mkdir(parents=True)

    remove_system_paths(paths)

    assert not paths.app_root.exists()
    assert not paths.config_root.exists()
    assert not paths.data_root.exists()
    assert not paths.log_root.exists()
    assert paths.drop_dir.exists()


def test_remove_system_paths_purges_ingest_drop_when_requested(tmp_path: Path) -> None:
    paths = SimpleNamespace(
        app_root=tmp_path / "missing-app",
        config_root=tmp_path / "missing-config",
        data_root=tmp_path / "missing-data",
        log_root=tmp_path / "missing-log",
        drop_dir=tmp_path / "srv" / "parseus" / "ftp",
        env_dir=tmp_path / "missing-config" / "envs",
        certs_dir=tmp_path / "missing-config" / "certs",
        compose_env_file=tmp_path / "missing-config" / "compose.env",
        current_manifest=tmp_path / "missing-data" / "state" / "current_manifest.json",
        runtime_compose=tmp_path / "missing-data" / "runtime" / "compose.runtime.yaml",
        images_compose=tmp_path / "missing-data" / "runtime" / "compose.images.yaml",
        wrapper_path=tmp_path / "usr" / "local" / "bin" / "manage-parseus",
    )
    paths.drop_dir.mkdir(parents=True)

    remove_system_paths(paths, purge_ingest_drop=True)

    assert not paths.drop_dir.exists()


def test_remove_system_paths_rejects_unmarked_app_root(tmp_path: Path) -> None:
    paths = SimpleNamespace(
        app_root=tmp_path / "opt" / "parseus",
        config_root=tmp_path / "missing-config",
        data_root=tmp_path / "missing-data",
        log_root=tmp_path / "missing-log",
        drop_dir=tmp_path / "srv" / "parseus" / "ftp",
        env_dir=tmp_path / "missing-config" / "envs",
        certs_dir=tmp_path / "missing-config" / "certs",
        compose_env_file=tmp_path / "missing-config" / "compose.env",
        current_manifest=tmp_path / "missing-data" / "state" / "current_manifest.json",
        runtime_compose=tmp_path / "missing-data" / "runtime" / "compose.runtime.yaml",
        images_compose=tmp_path / "missing-data" / "runtime" / "compose.images.yaml",
        wrapper_path=tmp_path / "usr" / "local" / "bin" / "manage-parseus",
    )
    paths.app_root.mkdir(parents=True)

    with pytest.raises(ManageParseusError, match="markers not found"):
        remove_system_paths(paths)


def test_parse_integrations_normalizes_dedupes_and_validates() -> None:
    assert parse_integrations_value(" GPIB,winlog,gpib ", source="test") == [
        "gpib",
        "winlog",
    ]
    assert parse_integrations_value("", source="test") == []
    assert parse_integrations_value(None, source="test") is None

    with pytest.raises(ManageParseusError):
        parse_integrations_value("unknown", source="test")


def test_workflow_integration_resolution_sources(capsys) -> None:
    explicit_args = SimpleNamespace(integrations="gpib", integrations_source="--integrations")
    assert resolve_install_integrations(explicit_args) == ["gpib"]

    manifest_args = SimpleNamespace(integrations=None, integrations_source="--integrations")
    assert resolve_existing_install_integrations({"integrations": ["winlog"]}, manifest_args) == [
        "winlog"
    ]
    assert resolve_existing_install_integrations(None, manifest_args) == []
    assert resolve_status_integrations({"integrations": ["gpib"]}, manifest_args) == (
        ["gpib"],
        "manifest",
    )

    captured = capsys.readouterr()
    assert "Reusing integrations from existing manifest" in captured.out


def test_build_installer_config_applies_file_and_cli_precedence(tmp_path: Path) -> None:
    config_file = tmp_path / "parseus.ini"
    config_file.write_text(
        "\n".join(
            [
                "[install]",
                "method = bundle",
                "integrations = gpib",
                "",
                "[nginx]",
                "https_port = 8443",
                "",
                "[compose]",
                "PARSEUS_EXTRA = yes",
                "",
            ]
        ),
        encoding="utf-8",
    )

    args = argparse.Namespace(
        config=config_file,
        method="repository",
        integrations="winlog",
    )

    config = build_installer_config(args)

    assert config.config_file == config_file
    assert config.method == "repository"
    assert config.integrations_arg == "winlog"
    assert config.project_name == "parseus"
    assert config.compose_env["PARSEUS_PROJECT_NAME"] == "parseus"
    assert config.https_port == 8443
    assert config.public_url == "https://127.0.0.1:8443/"
    assert config.compose_env["PARSEUS_HTTPS_PORT"] == "8443"
    assert config.compose_env["PARSEUS_EXTRA"] == "yes"


def test_build_installer_config_applies_project_and_certificate_sans(tmp_path: Path) -> None:
    config_file = tmp_path / "parseus.ini"
    config_file.write_text(
        "\n".join(
            [
                "[install]",
                "project_name = parseus_prod",
                "",
                "[certificates]",
                "common_name = parseus.example.com",
                "san_dns = parseus.example.com, parseus.internal",
                "san_ips = 127.0.0.1, ::1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    args = argparse.Namespace(
        config=config_file,
        method=None,
        integrations=None,
        project_name="parseus_cli",
    )

    config = build_installer_config(args)

    assert config.project_name == "parseus_cli"
    assert config.compose_env["PARSEUS_PROJECT_NAME"] == "parseus_cli"
    assert config.certificate_common_name == "parseus.example.com"
    assert config.certificate_san_dns == ["parseus.example.com", "parseus.internal"]
    assert config.certificate_san_ips == ["127.0.0.1", "::1"]


def test_build_installer_config_rejects_invalid_project_and_certificate_values(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "parseus.ini"
    config_file.write_text(
        "[install]\nproject_name = Bad.Name\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(
        config=config_file,
        method=None,
        integrations=None,
        project_name=None,
    )

    with pytest.raises(ManageParseusError, match="project name"):
        build_installer_config(args)

    config_file.write_text(
        "[certificates]\nsan_ips = not-an-ip\n",
        encoding="utf-8",
    )

    with pytest.raises(ManageParseusError, match="san_ips"):
        build_installer_config(args)


def test_build_installer_config_rejects_unknown_options(tmp_path: Path) -> None:
    config_file = tmp_path / "parseus.ini"
    config_file.write_text("[install]\nunknown = value\n", encoding="utf-8")

    args = argparse.Namespace(
        config=config_file,
        method=None,
        integrations=None,
    )

    with pytest.raises(ManageParseusError) as exc_info:
        build_installer_config(args)

    assert "Unsupported option" in exc_info.value.message


def test_read_project_version_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "parseus"\nversion = "2.3.4"\n',
        encoding="utf-8",
    )

    assert read_project_version(tmp_path) == "2.3.4"


def test_read_project_version_falls_back_to_dev(tmp_path: Path) -> None:
    assert read_project_version(tmp_path) == "dev"


def test_build_compose_env_adds_pyproject_version_without_overriding_config(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "2.3.4"\n',
        encoding="utf-8",
    )
    install_paths = SimpleNamespace(source_root=tmp_path)

    derived = build_compose_env(
        install_paths,
        InstallerConfig(compose_env={"PARSEUS_PROJECT_NAME": "parseus"}),
    )
    overridden = build_compose_env(
        install_paths,
        InstallerConfig(compose_env={"PARSEUS_VERSION": "9.9.9"}),
    )

    assert derived["PARSEUS_VERSION"] == "2.3.4"
    assert derived["PARSEUS_PROJECT_NAME"] == "parseus"
    assert overridden["PARSEUS_VERSION"] == "9.9.9"


def test_env_value_encoding_round_trips_quotes_and_backslashes(tmp_path: Path) -> None:
    env_path = tmp_path / "quoted.env"
    original = 'path \\\\server\\share and "quotes"'
    env_path.write_text(f"VALUE={encode_env_value(original)}\n", encoding="utf-8")

    assert parse_env_file(env_path)["VALUE"] == original


def test_env_file_generation_preserves_existing_values(monkeypatch, tmp_path: Path) -> None:
    env_path = tmp_path / "production.env"
    env_path.write_text('DJANGO_SECRET_KEY="existing"\nEMPTY=""\n', encoding="utf-8")

    # This test exercises default/generator merge and backup behavior, not the
    # root-only system permission path exercised separately in
    # test_write_env_file_applies_config_permissions_as_root. Force the
    # non-root branch so it does not depend on a real "parseus-admin" group
    # existing on the test runner (e.g. CI containers running as root).
    monkeypatch.setattr("manage_parseus_lib.install.env_files.os.geteuid", lambda: 1000)

    ensure_managed_env_file(
        env_path,
        defaults={"DJANGO_DEBUG": False, "EMPTY": "filled"},
        generators={"DJANGO_SECRET_KEY": lambda: "generated"},
        header="test env",
    )

    parsed = parse_env_file(env_path)
    assert parsed["DJANGO_SECRET_KEY"] == "existing"
    assert parsed["EMPTY"] == "filled"
    assert parsed["DJANGO_DEBUG"] == "False"
    assert list((tmp_path / ".backups").glob("production.env-*.bak"))


def test_env_overrides_cannot_set_generated_secrets() -> None:
    specs = get_env_file_specs()

    with pytest.raises(ManageParseusError) as exc_info:
        apply_env_overrides_to_specs(
            specs,
            {"production.env": {"DJANGO_SECRET_KEY": "not-allowed"}},
        )

    assert "Refusing to set generated secret" in exc_info.value.message


def test_django_host_and_origin_defaults_are_derived_from_public_url() -> None:
    assert django_host_and_origin_defaults("https://parseus.example.com/") == (
        "parseus.example.com",
        "https://parseus.example.com",
    )
    assert django_host_and_origin_defaults("https://parseus.example.com:8443/ui/") == (
        "parseus.example.com",
        "https://parseus.example.com:8443",
    )
    assert django_host_and_origin_defaults("https://127.0.0.1:8443/") == (
        "127.0.0.1,localhost",
        "https://127.0.0.1:8443,https://localhost:8443",
    )
    assert django_host_and_origin_defaults("https://localhost/") == (
        "localhost,127.0.0.1",
        "https://localhost,https://127.0.0.1",
    )


def test_production_env_defaults_include_public_django_host_and_origin() -> None:
    specs = get_env_file_specs(public_url="https://parseus.example.com:8443/")

    production_defaults = specs["production.env"]["defaults"]
    assert production_defaults["DJANGO_ALLOWED_HOSTS"] == "parseus.example.com"
    assert (
        production_defaults["DJANGO_CSRF_TRUSTED_ORIGINS"]
        == "https://parseus.example.com:8443"
    )
    assert production_defaults["DJANGO_CSRF_COOKIE_SECURE"] is True
    assert production_defaults["DJANGO_DEBUG"] is False
    assert production_defaults["DJANGO_SESSION_COOKIE_SECURE"] is True


def test_compose_helpers_extract_profiles_images_and_ports() -> None:
    assert build_compose_profile_args(["gpib", "winlog", "gpib"]) == [
        "--profile",
        "gpib",
        "--profile",
        "winlog",
    ]
    assert compose_base_cmd("parseus-prod") == [
        "docker",
        "compose",
        "--project-name",
        "parseus-prod",
    ]
    rendered = {
        "services": {
            "web": {"image": "parseus:latest"},
            "redis": {"image": "redis:7"},
            "no-image": {},
        },
        "volumes": {
            "database_data": {"name": "parseus_database_data"},
            "anonymous": {},
        },
    }
    assert compute_required_images(rendered) == ["parseus:latest", "redis:7"]
    assert compute_named_volumes(rendered) == ["anonymous", "parseus_database_data"]
    assert _normalize_compose_port_entry("127.0.0.1:8443:443") == [("127.0.0.1", 8443)]
    assert _normalize_compose_port_entry({"published": "8000-8002", "host_ip": "0.0.0.0"}) == [
        ("0.0.0.0", 8000),
        ("0.0.0.0", 8001),
        ("0.0.0.0", 8002),
    ]


def test_verify_required_bind_mounts_accepts_existing_paths(tmp_path: Path) -> None:
    config_file = tmp_path / "nginx.conf"
    state_dir = tmp_path / "state"
    config_file.write_text("events {}\n", encoding="utf-8")
    state_dir.mkdir()
    state_dir.chmod(0o777)
    rendered = {
        "services": {
            "nginx": {"volumes": [f"{config_file}:/etc/nginx/nginx.conf:ro"]},
            "logstash": {
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(state_dir),
                        "target": "/usr/share/logstash/data",
                        "read_only": False,
                    }
                ]
            },
        }
    }

    mounts = verify_required_bind_mounts(rendered)

    assert mounts == [
        (config_file, "/etc/nginx/nginx.conf", True),
        (state_dir, "/usr/share/logstash/data", False),
    ]


def test_verify_required_bind_mounts_rejects_missing_paths(tmp_path: Path) -> None:
    missing = tmp_path / "missing.conf"
    rendered = {
        "services": {
            "nginx": {"volumes": [f"{missing}:/etc/nginx/nginx.conf:ro"]},
        }
    }

    with pytest.raises(ManageParseusError, match="Missing bind mount source"):
        verify_required_bind_mounts(rendered)


def test_verify_required_bind_mounts_rejects_container_unwritable_paths(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.chmod(0o700)
    stat_result = state_dir.stat()
    monkeypatch.setattr(
        "manage_parseus_lib.docker.compose.CONTAINER_WRITABLE_UID",
        stat_result.st_uid + 100000,
    )
    monkeypatch.setattr(
        "manage_parseus_lib.docker.compose.CONTAINER_WRITABLE_GID",
        stat_result.st_gid + 100000,
    )
    rendered = {
        "services": {
            "logstash": {"volumes": [f"{state_dir}:/usr/share/logstash/data"]},
        }
    }

    with pytest.raises(ManageParseusError, match="container UID/GID"):
        verify_required_bind_mounts(rendered)


def test_path_writable_by_uid_gid_checks_owner_group_and_other_bits(tmp_path: Path) -> None:
    path = tmp_path / "state"
    path.mkdir()
    stat_result = path.stat()

    path.chmod(0o700)
    assert _path_writable_by_uid_gid(path, stat_result.st_uid, 123456) is True
    assert _path_writable_by_uid_gid(path, 123456, stat_result.st_gid) is False

    path.chmod(0o070)
    assert _path_writable_by_uid_gid(path, 123456, stat_result.st_gid) is True

    path.chmod(0o002)
    assert _path_writable_by_uid_gid(path, 123456, 654321) is True


def test_repository_image_commands_use_compose_env_file(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def fake_run_streaming(cmd, **kwargs):
        calls.append(cmd)
        return CmdResult(ok=True)

    compose_env = tmp_path / "compose.env"
    compose_env.write_text("PARSEUS_VERSION=2.3.4\n", encoding="utf-8")
    paths = SimpleNamespace(
        project_name="parseus",
        compose_env_file=compose_env,
        build_compose=tmp_path / "compose.build.yaml",
        images_compose=tmp_path / "compose.images.yaml",
    )
    monkeypatch.setattr("manage_parseus_lib.docker.images.run_streaming", fake_run_streaming)

    build_docker_images(paths)
    pull_docker_images(paths)

    assert calls[0][:5] == [
        "docker",
        "compose",
        "--project-name",
        "parseus",
        "--env-file",
    ]
    assert calls[0][5] == str(compose_env)
    assert calls[0][-3:] == ["build", "--progress", "plain"]
    assert calls[1][:6] == [
        "docker",
        "compose",
        "--project-name",
        "parseus",
        "--env-file",
        str(compose_env),
    ]
    assert calls[1][-1] == "pull"


def test_app_version_from_image_ref_returns_tags_only() -> None:
    assert app_version_from_image_ref("parseus:1.2.3") == "1.2.3"
    assert app_version_from_image_ref("parseus:1.2.3@sha256:abc123") == "1.2.3"
    assert app_version_from_image_ref("parseus") == ""
    assert app_version_from_image_ref("parseus@sha256:abc123") == ""


def test_resolve_app_version_prefers_compose_env() -> None:
    assert (
        resolve_app_version(
            InstallerConfig(compose_env={"PARSEUS_VERSION": "2.0.0"}),
            ["parseus:1.0.0"],
        )
        == "2.0.0"
    )


def test_resolve_app_version_falls_back_to_app_image_tag() -> None:
    assert resolve_app_version(InstallerConfig(), ["parseus:1.2.3"]) == "1.2.3"
    assert resolve_app_version(InstallerConfig(), []) == "unknown"


def test_manifest_helpers_validate_schema_and_avoid_raw_env_values(tmp_path: Path) -> None:
    validate_manifest_schema({"schema_version": 1}, tmp_path / "manifest.json")
    with pytest.raises(ManageParseusError):
        validate_manifest_schema({"schema_version": 999}, tmp_path / "manifest.json")

    config = InstallerConfig(
        compose_env={"PARSEUS_HTTPS_PORT": "8443", "SECRETISH": "raw-secret"},
        env_overrides={"production.env": {"SOME_SECRET": "raw-secret"}},
    )
    manifest_config = build_manifest_installer_config(config)

    assert manifest_config["compose_env_keys"] == ["PARSEUS_HTTPS_PORT", "SECRETISH"]
    assert manifest_config["project_name"] == "parseus"
    assert manifest_config["certificate_san_dns"] == ["localhost"]
    assert manifest_config["certificate_san_ips"] == ["127.0.0.1"]
    assert manifest_config["known_compose_values"] == {"PARSEUS_HTTPS_PORT": "8443"}
    assert "raw-secret" not in str(manifest_config)
    assert stable_config_fingerprint({"a": 1}) == stable_config_fingerprint({"a": 1})


def test_manifest_records_and_normalizes_named_volumes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "manage_parseus_lib.state.manifest.resolve_image_ids",
        lambda images: {image: f"sha256:{idx}" for idx, image in enumerate(images)},
    )

    system_account_data = system_accounts.SystemAccountProvisioning(
        created_service_user=True,
    ).as_manifest_data()
    manifest = build_install_manifest(
        ["parseus:latest"],
        ["gpib"],
        InstallerConfig(),
        named_volumes=["parseus_static_data", "parseus_static_data", ""],
        system_accounts=system_account_data,
    )

    assert manifest["app_version"] == "latest"
    assert manifest_named_volumes_value(manifest) == ["parseus_static_data"]
    assert manifest["system_accounts"] == system_account_data
    assert filter_volumes_by_suffix(
        manifest_named_volumes_value(manifest),
        ["_static_data"],
    ) == ["parseus_static_data"]


def test_build_install_manifest_records_compose_env_app_version(monkeypatch) -> None:
    monkeypatch.setattr(
        "manage_parseus_lib.state.manifest.resolve_image_ids",
        lambda images: {image: f"sha256:{idx}" for idx, image in enumerate(images)},
    )

    manifest = build_install_manifest(
        ["parseus:1.0.0"],
        [],
        InstallerConfig(compose_env={"PARSEUS_VERSION": "2.0.0"}),
    )

    assert manifest["app_version"] == "2.0.0"


def test_manifest_app_version_change_is_meaningful() -> None:
    old_manifest = {"schema_version": 1, "app_version": "1.0.0"}
    new_manifest = {"schema_version": 1, "app_version": "2.0.0"}

    assert manifests_differ_meaningfully(old_manifest, new_manifest) is True


def test_build_stack_plan_includes_named_volumes(monkeypatch) -> None:
    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.validate_compose_files",
        lambda install_paths, integrations: None,
    )
    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.get_rendered_compose_config",
        lambda install_paths, integrations: {
            "services": {"web": {"image": "parseus:latest"}},
            "volumes": {"static_data": {"name": "parseus_static_data"}},
        },
    )

    plan = build_stack_plan(SimpleNamespace(), [])

    assert plan.required_images == ["parseus:latest"]
    assert plan.named_volumes == ["parseus_static_data"]


def test_verify_stack_plan_can_skip_duplicate_image_verification(monkeypatch) -> None:
    calls = []
    paths = SimpleNamespace()
    plan = StackPlan(
        rendered={"services": {}},
        required_images=["parseus:latest"],
        named_volumes=[],
    )

    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.verify_required_ports",
        lambda rendered: calls.append(("ports", rendered)),
    )
    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.verify_required_bind_mounts",
        lambda rendered: calls.append(("binds", rendered)),
    )
    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.verify_required_images",
        lambda images: calls.append(("images", images)),
    )

    verify_stack_plan(paths, [], plan, verify_images=False)

    assert calls == [("ports", {"services": {}}), ("binds", {"services": {}})]


def test_run_app_setup_runs_migrations_and_bootstrap_commands(monkeypatch, tmp_path: Path) -> None:
    calls = []
    paths = SimpleNamespace(
        project_name="parseus-prod",
        compose_env_file=tmp_path / "missing.env",
        images_compose=Path("compose.images.yaml"),
        runtime_compose=Path("compose.runtime.yaml"),
    )

    def fake_run_streaming(cmd, timeout, heartbeat_message):
        calls.append((cmd, timeout, heartbeat_message))
        return SimpleNamespace(ok=True, stdout="done", stderr="")

    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.run_streaming",
        fake_run_streaming,
    )

    run_app_setup(paths, ["gpib"])

    assert [call[0][-3:] for call in calls] == [
        ["parseus", "migrate", "--noinput"],
        ["web", "parseus", "populate_analytics"][-3:],
        ["web", "parseus", "create_periodic_tasks"][-3:],
    ]
    assert calls[0][0] == [
        "docker",
        "compose",
        "--project-name",
        "parseus-prod",
        "--profile",
        "gpib",
        "-f",
        "compose.images.yaml",
        "-f",
        "compose.runtime.yaml",
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "web",
        "parseus",
        "migrate",
        "--noinput",
    ]
    assert all(call[1] == 60 * 60 for call in calls)
    assert all("Django database setup" in call[2] for call in calls)


def test_run_app_setup_fails_when_database_setup_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.run_streaming",
        lambda cmd, timeout, heartbeat_message: SimpleNamespace(
            ok=False,
            stdout="",
            stderr="migrate failed",
        ),
    )

    with pytest.raises(ManageParseusError, match="Failed to apply Django database setup"):
        run_app_setup(
            SimpleNamespace(
                project_name=None,
                compose_env_file=Path("missing.env"),
                images_compose=Path("compose.images.yaml"),
                runtime_compose=Path("compose.runtime.yaml"),
            ),
            [],
        )


def test_start_stack_and_wait_runs_setup_before_readiness(monkeypatch, tmp_path: Path) -> None:
    calls = []
    config = InstallerConfig(
        certificate_cert_file=tmp_path / "cert.pem",
        certificate_key_file=tmp_path / "key.pem",
    )
    paths = SimpleNamespace()

    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.bring_up_stack",
        lambda install_paths, integrations: calls.append("up"),
    )
    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.run_app_setup",
        lambda install_paths, integrations: calls.append("setup"),
    )
    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.show_stack_status",
        lambda install_paths, integrations: calls.append("status"),
    )
    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.wait_for_port",
        lambda host, port, timeout: calls.append(("port", host, port, timeout)),
    )
    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.wait_for_https_ready",
        lambda url, timeout, allow_self_signed: calls.append(
            ("https", url, timeout, allow_self_signed)
        ),
    )

    start_stack_and_wait(paths, config, ["gpib"])

    assert calls == [
        "setup",
        "up",
        "status",
        ("port", "127.0.0.1", 443, 60),
        ("https", "https://127.0.0.1/", 90, False),
    ]


def test_start_stack_and_wait_can_warn_on_readiness_failure(monkeypatch, capsys) -> None:
    calls = []
    config = InstallerConfig()
    paths = SimpleNamespace()

    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.run_app_setup",
        lambda install_paths, integrations: calls.append("setup"),
    )
    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.bring_up_stack",
        lambda install_paths, integrations: calls.append("up"),
    )
    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.show_stack_status",
        lambda install_paths, integrations: calls.append("status"),
    )

    def fail_port(host, port, timeout):
        calls.append(("port", host, port, timeout))
        raise ManageParseusError("probe blocked")

    monkeypatch.setattr("manage_parseus_lib.workflows.stack.wait_for_port", fail_port)

    start_stack_and_wait(paths, config, [], require_readiness=False)

    assert calls == ["setup", "up", "status", ("port", "127.0.0.1", 443, 60)]
    assert (
        "local readiness check did not complete successfully" in capsys.readouterr().err
    )


def test_start_stack_and_wait_requires_readiness_by_default(monkeypatch) -> None:
    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.run_app_setup",
        lambda *_: None,
    )
    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.bring_up_stack",
        lambda *_: None,
    )
    monkeypatch.setattr(
        "manage_parseus_lib.workflows.stack.show_stack_status",
        lambda *_: None,
    )

    def fail_port(*args, **kwargs):
        raise ManageParseusError("probe blocked")

    monkeypatch.setattr("manage_parseus_lib.workflows.stack.wait_for_port", fail_port)

    with pytest.raises(ManageParseusError, match="probe blocked"):
        start_stack_and_wait(SimpleNamespace(), InstallerConfig(), [])


# ---------------------------------------------------------------------------
# systemd unit lifecycle
# ---------------------------------------------------------------------------

from manage_parseus_lib.system import systemd as systemd_module


def _systemd_paths(tmp_path: Path, integrations_project: str = "parseus") -> SimpleNamespace:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        project_name=integrations_project,
        runtime_root=runtime_root,
        compose_env_file=tmp_path / "compose.env",
        images_compose=runtime_root / "compose.images.yaml",
        runtime_compose=runtime_root / "compose.runtime.yaml",
    )


def _fake_systemctl_recorder(monkeypatch, *, ok: bool = True, outputs=None):
    """Patch systemd_module.run to record systemctl invocations."""
    calls = []
    outputs = outputs or {}

    def fake_run(cmd, timeout=None):
        calls.append(cmd)
        # cmd is ["systemctl", <subcommand>, ...]
        sub = cmd[1] if len(cmd) > 1 else ""
        stdout = outputs.get(sub, "")
        return CmdResult(ok=ok, stdout=stdout, stderr="" if ok else "boom", rc=0 if ok else 1)

    monkeypatch.setattr(systemd_module, "run", fake_run)
    return calls


def test_systemd_available_reflects_systemctl_on_path(monkeypatch) -> None:
    monkeypatch.setattr(systemd_module.shutil, "which", lambda name: "/usr/bin/systemctl")
    assert systemd_module.systemd_available() is True
    monkeypatch.setattr(systemd_module.shutil, "which", lambda name: None)
    assert systemd_module.systemd_available() is False


def test_install_systemd_unit_noop_without_systemctl(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(systemd_module.shutil, "which", lambda name: None)
    calls = _fake_systemctl_recorder(monkeypatch)

    systemd_module.install_systemd_unit(_systemd_paths(tmp_path), ["gpib"])

    assert calls == []  # never shells out to systemctl on a non-systemd host
    assert "skipping systemd unit installation" in capsys.readouterr().out


def test_install_systemd_unit_writes_enables_and_renders(monkeypatch, tmp_path: Path) -> None:
    def fake_which(name):
        return "/usr/bin/systemctl" if name == "systemctl" else "/usr/bin/docker"

    monkeypatch.setattr(systemd_module.shutil, "which", fake_which)
    unit_path = tmp_path / "parseus.service"
    monkeypatch.setattr(systemd_module, "SYSTEMD_UNIT_PATH", unit_path)
    calls = _fake_systemctl_recorder(monkeypatch)

    paths = _systemd_paths(tmp_path)
    systemd_module.install_systemd_unit(paths, ["gpib", "winlog"])

    # Unit file written with the same compose invocation the script would use.
    unit_text = unit_path.read_text()
    assert "Description=Parseus Docker Compose stack" in unit_text
    assert "Type=oneshot" in unit_text
    assert "RemainAfterExit=yes" in unit_text
    assert f"--project-name {paths.project_name}" in unit_text
    assert f"-f {paths.images_compose}" in unit_text
    assert f"-f {paths.runtime_compose}" in unit_text
    assert "up --no-build -d" in unit_text
    assert "WantedBy=multi-user.target" in unit_text

    # Env file carries resolved COMPOSE_PROFILES.
    env_text = (paths.runtime_root / systemd_module.SYSTEMD_ENV_FILENAME).read_text()
    assert "COMPOSE_PROFILES=gpib,winlog" in env_text

    assert ["systemctl", "daemon-reload"] in calls
    assert ["systemctl", "enable", "parseus.service"] in calls


def test_install_systemd_unit_warns_when_enable_fails(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(systemd_module.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(systemd_module, "SYSTEMD_UNIT_PATH", tmp_path / "parseus.service")
    _fake_systemctl_recorder(monkeypatch, ok=False)

    systemd_module.install_systemd_unit(_systemd_paths(tmp_path), [])

    assert "could not enable it" in capsys.readouterr().err


def test_write_systemd_integration_env_only_touches_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(systemd_module.shutil, "which", lambda name: "/usr/bin/systemctl")
    calls = _fake_systemctl_recorder(monkeypatch)
    paths = _systemd_paths(tmp_path)

    systemd_module.write_systemd_integration_env(paths, ["winlog"])

    env_text = (paths.runtime_root / systemd_module.SYSTEMD_ENV_FILENAME).read_text()
    assert "COMPOSE_PROFILES=winlog" in env_text
    assert calls == []  # no daemon-reload/enable for a cheap profile refresh


def test_start_and_stop_via_systemd(monkeypatch) -> None:
    monkeypatch.setattr(systemd_module.shutil, "which", lambda name: "/usr/bin/systemctl")
    calls = _fake_systemctl_recorder(monkeypatch)

    assert systemd_module.start_via_systemd() is True
    assert systemd_module.stop_via_systemd() is True
    assert ["systemctl", "start", "parseus.service"] in calls
    assert ["systemctl", "stop", "parseus.service"] in calls


def test_start_stop_via_systemd_report_false_without_systemctl(monkeypatch) -> None:
    monkeypatch.setattr(systemd_module.shutil, "which", lambda name: None)
    calls = _fake_systemctl_recorder(monkeypatch)

    assert systemd_module.start_via_systemd() is False
    assert systemd_module.stop_via_systemd() is False
    assert calls == []


def test_remove_systemd_unit_disables_and_unlinks(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(systemd_module.shutil, "which", lambda name: "/usr/bin/systemctl")
    unit_path = tmp_path / "parseus.service"
    unit_path.write_text("[Unit]\n")
    monkeypatch.setattr(systemd_module, "SYSTEMD_UNIT_PATH", unit_path)
    calls = _fake_systemctl_recorder(monkeypatch)

    systemd_module.remove_systemd_unit()

    assert not unit_path.exists()
    assert ["systemctl", "disable", "--now", "parseus.service"] in calls
    assert ["systemctl", "daemon-reload"] in calls


def test_systemd_status_facts_none_without_systemctl(monkeypatch) -> None:
    monkeypatch.setattr(systemd_module.shutil, "which", lambda name: None)
    assert systemd_module.systemd_status_facts() is None


def test_systemd_status_facts_reports_enabled_and_active(monkeypatch) -> None:
    monkeypatch.setattr(systemd_module.shutil, "which", lambda name: "/usr/bin/systemctl")
    _fake_systemctl_recorder(
        monkeypatch,
        outputs={"is-enabled": "enabled", "is-active": "active"},
    )

    facts = systemd_module.systemd_status_facts()
    assert facts == {"enabled": "enabled", "active": "active"}

