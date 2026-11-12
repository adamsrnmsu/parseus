from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

from manage_parseus_lib.commands.config import do_sample_config, do_validate_config
from manage_parseus_lib.commands.install import do_install
from manage_parseus_lib.commands.keys import do_create_key
from manage_parseus_lib.commands.runtime import do_down, do_restart, do_up
from manage_parseus_lib.commands.status import do_status
from manage_parseus_lib.commands.uninstall import do_uninstall
from manage_parseus_lib.commands.upgrade import do_upgrade
from manage_parseus_lib.config.loader import apply_installer_config_to_args, build_installer_config
from manage_parseus_lib.context import DeploymentContext
from manage_parseus_lib.errors import ManageParseusError
from manage_parseus_lib.install.paths import build_paths, require_linux
from manage_parseus_lib.output import print_error

def get_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Install, upgrade, or uninstall Parseus. "
            "Install and upgrade support either repository-based deployment "
            "(from a cloned repository) or bundle-based deployment "
            "(from pre-packaged local image archives)."
        )
    )

    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Optional installer configuration file. Values in this file override "
            "built-in defaults; explicit CLI options override the file."
        ),
    )
    common_parser.add_argument(
        "--integrations",
        dest="integrations",
        type=str,
        default=None,
        help=(
            "Comma-separated integrations to enable, for example: gpib,winlog,zeek. "
            "If omitted, install enables no integrations. "
            "Upgrade reuses integrations from the current manifest when available."
        ),
    )
    common_parser.add_argument(
        "--project-name",
        dest="project_name",
        type=str,
        default=None,
        help=(
            "Docker Compose project/container/volume name prefix. "
            "Default: parseus. Use a unique value for multiple installations "
            "on one host."
        ),
    )

    subparsers = parser.add_subparsers(
        title="commands",
        dest="command",
        required=True,
    )

    install_subparser = subparsers.add_parser(
        "install",
        parents=[common_parser],
        help="Install Parseus from a cloned repository or a pre-packaged bundle.",
        description=(
            "Install Parseus.\n\n"
            "Repository-based installation uses a cloned repository on the current system "
            "to build and pull images.\n"
            "Bundle-based installation uses pre-packaged local image archives."
        ),
    )
    install_subparser.set_defaults(func=do_install)
    install_subparser.add_argument(
        "--method",
        choices=["bundle", "repository"],
        type=str.lower,
        default=None,
        help=(
            "Installation method: 'repository' installs from a cloned repository; "
            "'bundle' installs from local image archives in the bundle. "
            "Default: repository."
        ),
    )
    install_subparser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open a web browser to the Parseus URL after a successful install.",
    )

    upgrade_subparser = subparsers.add_parser(
        "upgrade",
        parents=[common_parser],
        help="Upgrade an existing Parseus installation using updated images and configuration.",
        description=(
            "Upgrade Parseus to a newer version. "
            "This updates images and restarts the stack. "
            "By default, obsolete images recorded in the previous manifest are removed. "
            "Use --keep-old-images to retain them."
        ),
    )
    upgrade_subparser.set_defaults(func=do_upgrade)
    upgrade_subparser.add_argument(
        "--method",
        choices=["bundle", "repository"],
        type=str.lower,
        default=None,
        help=(
            "Upgrade method: 'repository' upgrades from a cloned repository; "
            "'bundle' upgrades from local image archives in the bundle. "
            "Default: repository."
        ),
    )
    upgrade_subparser.add_argument(
        "--keep-old-images",
        action="store_true",
        help="Keep obsolete images recorded in the previous install manifest.",
    )
    upgrade_subparser.add_argument(
        "--allow-downgrade",
        action="store_true",
        help="Allow upgrading to an older Parseus app version.",
    )
    upgrade_subparser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open a web browser to the Parseus URL after a successful upgrade.",
    )

    uninstall_subparser = subparsers.add_parser(
        "uninstall",
        parents=[common_parser],
        help="Uninstall Parseus and remove containers, volumes, and manifest-recorded images by default.",
        description=(
            "Uninstall Parseus. This stops and removes the Parseus containers, "
            "removes associated volumes and installer-managed files, "
            "and by default removes Docker images recorded in the install manifest. "
            "Use --keep-images to retain those images."
        ),
    )
    uninstall_subparser.set_defaults(func=do_uninstall)
    uninstall_subparser.add_argument(
        "--keep-images",
        action="store_true",
        help="Retain Docker images recorded in the install manifest.",
    )
    uninstall_subparser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive uninstall confirmation prompt.",
    )
    uninstall_subparser.add_argument(
        "--purge-ingest-drop",
        action="store_true",
        help="Also remove the Winlog ingest drop directory under /srv/parseus/ftp.",
    )
    uninstall_subparser.add_argument(
        "--remove-system-account",
        action="store_true",
        help="Remove installer-created parseus system user/groups when safe.",
    )

    up_subparser = subparsers.add_parser(
        "up",
        parents=[common_parser],
        help="Start an existing Parseus installation.",
        description=(
            "Start the Parseus Docker Compose stack for an existing installation. "
            "If a manifest exists, integrations are reused unless overridden with --integrations."
        ),
    )
    up_subparser.set_defaults(func=do_up)
    up_subparser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open a web browser to the Parseus URL after a successful startup.",
    )

    down_subparser = subparsers.add_parser(
        "down",
        parents=[common_parser],
        help="Stop an existing Parseus installation.",
        description=(
            "Stop the Parseus Docker Compose stack for an existing installation. "
            "This does not remove volumes, images, manifests, or generated files."
        ),
    )
    down_subparser.set_defaults(func=do_down)

    restart_subparser = subparsers.add_parser(
        "restart",
        parents=[common_parser],
        help="Restart an existing Parseus installation.",
        description=(
            "Restart the Parseus Docker Compose stack for an existing installation. "
            "If a manifest exists, integrations are reused unless overridden with --integrations."
        ),
    )
    restart_subparser.set_defaults(func=do_restart)
    restart_subparser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open a web browser to the Parseus URL after a successful restart.",
    )

    status_subparser = subparsers.add_parser(
        "status",
        parents=[common_parser],
        help="Show installation, manifest, and Docker Compose status.",
        description=(
            "Show Parseus installation status, selected manifest metadata, "
            "readiness endpoint information, and Docker Compose service status "
            "when Compose files and Docker are available."
        ),
    )
    status_subparser.set_defaults(func=do_status)

    sample_config_subparser = subparsers.add_parser(
        "sample-config",
        help="Print a documented sample installer configuration file.",
        description=(
            "Print a documented INI-style installer configuration template to "
            "stdout. Redirect the output to create a starter config file."
        ),
    )
    sample_config_subparser.set_defaults(func=do_sample_config)

    validate_config_subparser = subparsers.add_parser(
        "validate-config",
        parents=[common_parser],
        help="Validate installer configuration and print effective settings.",
        description=(
            "Parse and validate installer configuration, apply supported CLI "
            "overrides, print effective settings, and exit without touching "
            "Docker or installed files."
        ),
    )
    validate_config_subparser.set_defaults(func=do_validate_config)
    validate_config_subparser.add_argument(
        "--method",
        choices=["bundle", "repository"],
        type=str.lower,
        default=None,
        help="Override installation method for validation: repository or bundle.",
    )

    create_key_subparser = subparsers.add_parser(
        "create-key",
        help="Create installer-managed security keys (minimal requirements).",
        description=(
            "Create a security key for Parseus installer configuration. "
            "This command avoids installer/network/docker behavior and "
            "only outputs the requested key value."
        ),
    )
    create_key_subparser.set_defaults(func=do_create_key)
    create_key_subparser.add_argument(
        "--kind",
        choices=["fernet", "django-secret"],
        required=True,
        help="Which key to create.",
    )

    return parser.parse_args(argv)


def run_cli(argv: Optional[List[str]] = None) -> None:
    args = get_args(argv)

    # Keep minimal requirements for commands that do not need installer state.
    if args.command in {"create-key", "sample-config"}:
        args.func(args)
        return

    if args.command == "validate-config":
        config = build_installer_config(args)
        apply_installer_config_to_args(args, config)
        args.installer_config = config
        args.func(args)
        return

    config = build_installer_config(args)
    apply_installer_config_to_args(args, config)
    require_linux()
    install_paths = build_paths(project_name=config.project_name)
    args.deployment_context = DeploymentContext(config=config, paths=install_paths)
    args.installer_config = config
    args.install_paths = install_paths

    args.func(args)


def main(argv: Optional[List[str]] = None) -> int:
    try:
        run_cli(argv)
    except ManageParseusError as e:
        print_error(e.message)
        return e.rc
    return 0

