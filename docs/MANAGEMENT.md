# Parseus Management Guide

This guide covers installing, upgrading, running, and uninstalling Parseus with `manage_parseus.py`.

## Supported platforms

The management script supports Linux (x86_64 / AMD64 only). ARM architectures are not supported.

## Python versions

Parseus has two separate Python requirements:

- **Management script (this guide):** `manage_parseus.py` / `manage-parseus` installs Parseus and brings up the containers. It runs on the host with Python 3.8 or later and uses only the standard library, so no Python packages need to be installed on the host.
- **Application source code:** the `parseus` package requires Python 3.11 - 3.13 (`requires-python` in `pyproject.toml`). It runs inside the Docker images, so a deployment host does not need this version. It only matters when developing or running the source directly; see [DEVELOPMENT.md](DEVELOPMENT.md).

## Supported workflows

- Repository-based installation from a cloned repository
- Bundle-based/offline installation from pre-packaged Docker image archives
- Upgrade of an existing installation
- Runtime management: start, stop, restart, and status inspection
- Configuration template generation and validation
- Uninstall and cleanup

## What the management script does

Depending on the command, `manage_parseus.py` or the installed `manage-parseus` wrapper may:

- copy application files and documentation into `/opt/parseus`
- generate managed environment files under `/etc/parseus/envs`
- generate self-signed HTTPS certificates or install provided HTTPS certificates under `/etc/parseus/certs`
- validate Docker Compose configuration
- check required host ports
- build, pull, or load Docker images
- start, stop, or tear down the Docker Compose stack
- install and enable a `parseus.service` systemd unit (on hosts with systemd) so the stack starts on boot, and drive start/stop through systemd
- apply Django database migrations and refresh installer-managed bootstrap data after install or upgrade
- update the installation manifest when runtime configuration changes
- remove installed files, Docker volumes, and manifest-recorded images during uninstall
- record installation inventory and selected effective installer settings in `/var/lib/parseus/state/current_manifest.json`

## Requirements

### Minimum recommended hardware

- 8 CPU cores
- 16 GB RAM
- 20 GB of free disk space

Actual storage needs may be higher depending on retained Docker images, application data, logs, container volumes, and optional components.

### Repository-based install or upgrade

- Python 3.8 or later for the management script (not the application's 3.11 - 3.13 requirement)
- Docker
- Docker Compose v2 (`docker compose`)
- OpenSSL, unless external TLS certificate/key files are configured

### Bundle workflow

Packaging system, used to create the bundle:

- Docker
- Docker Compose v2 (`docker compose`)
- GNU Make

Installation system, used to install and run Parseus:

- Python 3.8 or later for the management script (not the application's 3.11 - 3.13 requirement)
- Docker
- Docker Compose v2 (`docker compose`)
- OpenSSL, unless external TLS certificate/key files are configured

## Installation

Install from a cloned repository:

    sudo python3 manage_parseus.py install

Install from an extracted bundle:

    sudo python3 manage_parseus.py install --method bundle

Default system-wide layout:

    /opt/parseus                  application files, bundled docs, integration assets
    /etc/parseus                  configuration, env files, TLS certificates
    /var/lib/parseus              installer state, manifest history, runtime Compose files
    /srv/parseus/ftp              optional Winlog ingest drop directory
    /var/log/parseus              Parseus host logs
    /usr/local/bin/manage-parseus management command wrapper
    /etc/systemd/system/parseus.service  systemd unit (created on hosts with systemd)

After installation, check status:

    sudo manage-parseus status

`status`, `up`, `down`, and `restart` can be run with `sudo`, or by a member of the `parseus-admin` group without `sudo`. `install`, `upgrade`, and `uninstall` require root. The stateless helper commands `sample-config`, `create-key`, and `validate-config` need no special privileges.

Optionally, add an administrator to the `parseus-admin` group so they can run status and runtime commands without `sudo`:

    sudo usermod -aG parseus-admin <user>

The user must log out and back in for the new group membership to take effect. If `sudo manage-parseus` reports "command not found", your sudo configuration may exclude `/usr/local/bin`; use the full path `sudo /usr/local/bin/manage-parseus <command>` instead.

Enable optional integrations:

    sudo python3 manage_parseus.py install --integrations gpib,winlog

Open a web browser to Parseus after install completes:

    sudo python3 manage_parseus.py install --open-browser

If `--integrations` is omitted during installation, no integrations are enabled.

Repository-based installation builds the Parseus application image using `compose.build.yaml`, pulls required supporting images using the managed runtime Compose files under `/var/lib/parseus/runtime/`, copies application files and documentation into `/opt/parseus`, writes configuration under `/etc/parseus`, validates the rendered Docker Compose configuration, applies Django database setup in one-off `web` containers, starts the stack, records the installation manifest, and then performs a local readiness check. Database setup runs `parseus migrate --noinput`, `parseus populate_analytics`, and `parseus create_periodic_tasks` using the managed Docker Compose configuration.

Bundle-based installation loads Docker image archives from the bundle `images/` directory and starts the stack without building images locally. Bundle installs run the same pre-start Django database setup and post-start readiness behavior.

The local readiness check is a post-start verification, not the source of truth for whether files were installed. If local security software, proxy settings, firewall rules, or certificate inspection prevents the management script from probing the HTTPS endpoint after the stack starts, the installer writes the manifest and completes with a warning. In that case, open the public URL in a browser and run `sudo manage-parseus status` to inspect the runtime markers and Docker Compose services.

The installed application layout preserves documentation in the same locations as the repository and bundle under `/opt/parseus`: `README.md`, focused guides under `docs/`, and optional-component documentation under `deploy/integrations/`.

## Optional integrations

Available integrations are:

- `gpib` — enables the combined GPIB-over-Ethernet listener/parser/indexer service; see [GPIB ingestor guide](../deploy/integrations/gpib/README.md)
- `winlog` — enables Windows event file ingestion through Logstash and a one-shot Elasticsearch ILM setup job; see [Windows Logstash ingestion guide](../deploy/integrations/winlog/README.md)

Pass integrations as a comma-separated list:

    sudo manage-parseus install --integrations gpib,winlog

The management script maps these integrations to the matching Docker Compose profiles and records the selected integrations in the installation manifest. The shared `ingestors` Compose profile is present for direct Compose users, but the supported management-script integration names are `gpib` and `winlog`.

After enabling an integration, run its verification helper from the installed root or repository root:

    bash /opt/parseus/deploy/integrations/gpib/scripts/verify_gpib_runtime.sh
    bash /opt/parseus/deploy/integrations/winlog/scripts/verify_winlog_runtime.sh

The integrations include hardened runtime behavior such as read-only mounts for bundled code/config where possible, non-root application containers, explicit container health checks, bounded logging/runtime state, and documented retention expectations. Review each integration's README before production deployment because both integrations can process sensitive operational data.

## Installer configuration file

Most deployment settings can be supplied with an INI-style configuration file:

    sudo manage-parseus install --config parseus-install.ini

Generate a documented starter configuration:

    manage-parseus sample-config > parseus-install.ini

Validate a configuration before installing or upgrading:

    sudo manage-parseus validate-config --config parseus-install.ini

Configuration file values override built-in defaults. Explicit CLI options such as `--method` and `--integrations` override values from the configuration file. Parseus uses the fixed system-wide layout above.

The installer validates configuration files strictly. Unknown sections and unknown options in fixed sections fail fast instead of being silently ignored. Arbitrary variable names are allowed only in `[compose]` and supported `[env.*]` sections. The `validate-config` command parses the same configuration, applies supported CLI overrides, prints effective settings, and exits without touching Docker or installed files.

Example configuration:

    [install]
    method = repository
    integrations = gpib,winlog
    project_name = parseus_prod

    [nginx]
    https_port = 8443

    [certificates]
    common_name = parseus.local
    san_dns = parseus.local,localhost
    san_ips = 127.0.0.1
    days = 825

    [compose]
    # Host drop directory used by the winlog Logstash file input.
    PARSEUS_FTP_HOST_DIR = /srv/parseus/ftp

    [env.datastore]
    DATASTORE_HOST_URL = http://elasticsearch.local:9200

    [env.production]
    DJANGO_DEBUG = false
    DJANGO_SESSION_COOKIE_SECURE = true
    DJANGO_CSRF_COOKIE_SECURE = true
    # Defaults for DJANGO_ALLOWED_HOSTS and DJANGO_CSRF_TRUSTED_ORIGINS
    # are derived from [network] public_url unless overridden here.

    [env.ingestor]
    INGESTOR_GPIB_IP = 10.0.0.50
    INGESTOR_GPIB_PORT = 1234

Supported top-level sections are:

- `[install]` — `method`, `integrations`, and `project_name`
- `[nginx]` — simple nginx host binding settings: `http_bind`, `http_port`, `https_bind`, and `https_port`
- `[network]` — advanced readiness/browser settings: `public_url`, `health_host`, and `https_port`
- `[certificates]` — generated self-signed certificate settings: `common_name`, `san_dns`, `san_ips`, and `days`, or external certificate settings: `cert_file` and `key_file`
- `[compose]` — Docker Compose interpolation variables written to `/etc/parseus/compose.env`
- `[env.datastore]`, `[env.production]`, `[env.ingestor]`, and `[env.ingestor_health]` — defaults for installer-managed env files

Environment-file config values are applied as managed defaults and do not overwrite existing non-empty values during upgrade. Generated secrets such as `DJANGO_SECRET_KEY` and `DJANGO_ENCRYPTION_KEY` cannot be set from the installer configuration file; use `create-key` when you need to generate secret values.

For production deployments, set `[network] public_url` to the HTTPS URL users will open. The installer derives `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` in `/etc/parseus/envs/production.env` from that public URL. Override those keys in `[env.production]` only when you need multiple hostnames or trusted origins. The installer also enables secure Django session and CSRF cookies by default through `DJANGO_SESSION_COOKIE_SECURE=true` and `DJANGO_CSRF_COOKIE_SECURE=true`; nginx additionally marks proxied cookies as `Secure` on the HTTPS virtual host.

Before rewriting an existing installer-managed env file, the installer saves a timestamped backup if the file contents are changing. Env file backups are stored in a `.backups` directory next to the file, such as `/etc/parseus/envs/.backups/` for service env files or `/etc/parseus/.backups/` for `/etc/parseus/compose.env`.

The installation manifest records selected effective installer settings, including the config file path, application bundle directory, method, Docker Compose project name, public URL, readiness host/port, certificate settings, rendered Docker volume names, configured Compose variable names, known nginx Compose values, env override file names, env override keys, system account metadata, and fingerprints of configured Compose/env override values. Arbitrary Compose and env override values are not copied into the manifest to avoid recording accidental secrets.

### Configuring HTTPS certificates

By default, install and upgrade generate self-signed nginx HTTPS certificates if installed certificate files do not already exist. Configure `[certificates] common_name`, `san_dns`, `san_ips`, and `days` to control the generated self-signed certificate:

    [certificates]
    common_name = parseus.local
    san_dns = parseus.local,localhost
    san_ips = 127.0.0.1
    days = 825

The generated certificate must have at least one DNS or IP subject alternative name. `san_dns` and `san_ips` are comma-separated lists. Certificate names may not contain control characters, and IP subject alternative names must be valid IPv4 or IPv6 addresses.

For production deployments, you can provide an existing certificate and private key:

    [certificates]
    cert_file = /etc/ssl/certs/parseus.crt
    key_file = /etc/ssl/private/parseus.key

`cert_file` and `key_file` must be provided together. The installer copies them into the installation `certs/` directory using the filenames expected by the bundled Docker Compose/nginx configuration. Existing installed certificate files are backed up under `certs/.backups/` before being replaced when the provided contents differ. The installed private key is written with restrictive permissions.

When external certificate files are configured, OpenSSL is not required for install or upgrade because the installer does not generate a self-signed certificate.

### Configuring nginx port forwarding

For most deployments, configure nginx host bindings through `[nginx]`:

    [nginx]
    https_port = 8443

This derives the matching Docker Compose variables and updates the default readiness/browser settings, so the installer checks and prints `https://127.0.0.1:8443/`.

You can configure the full HTTP and HTTPS binding set when needed:

    [nginx]
    http_bind = 127.0.0.1
    http_port = 8080
    https_bind = 0.0.0.0
    https_port = 8443

Wildcard binds such as `0.0.0.0` are used for Docker port publishing, but the installer still uses `127.0.0.1` for local readiness checks and the default browser URL. If users should open a DNS name, reverse proxy URL, or another externally visible address, override `[network]` explicitly:

    [network]
    public_url = https://parseus.example.com/
    health_host = 127.0.0.1
    https_port = 8443

Advanced users can still provide the lower-level Compose variables directly through `[compose]`. Values in `[compose]` override values derived from `[nginx]`.

### Docker Compose project names

By default, managed Docker containers and volumes use the `parseus` project/name prefix. If you run more than one Parseus installation on the same Docker host, configure a unique project name for each installation:

    [install]
    project_name = parseus_prod

You can also pass `--project-name` on install, upgrade, runtime, status, and uninstall commands. The installer writes the effective project name into the managed Compose `.env` file as `PARSEUS_PROJECT_NAME`, uses it with `docker compose --project-name`, and records rendered Docker volume names in the manifest. Uninstall prefers manifest-recorded volume names, reducing the risk of deleting resources that belong to another installation. Older manifests that do not record volumes fall back to the legacy `parseus_*` volume names and print a warning.

## Bundle workflow

Create a bundle on a packaging system:

    make package

This creates a versioned tarball under `dist/`:

    dist/parseus-<version>.tar.gz

For example, version `2026.11.02` produces `dist/parseus-2026.11.02.tar.gz`.

Transfer the tarball to the target system, extract it, and install from the extracted directory:

    tar -xzvf parseus-<version>.tar.gz
    cd parseus-<version>
    sudo python3 manage_parseus.py install --method bundle

Bundle installs load Docker image archives from the bundle `images/` directory and do not build images locally.

## Upgrade

Upgrade must be run from the new release (a cloned repository or an extracted bundle), not from the installed `/usr/local/bin/manage-parseus` symlink. The installed copy is the upgrade destination; running it from there would copy files onto themselves, so `upgrade` refuses to run and exits with an error.

Upgrade from a cloned repository:

    cd /path/to/parseus
    sudo python3 manage_parseus.py upgrade

Upgrade from an extracted bundle:

    cd /path/to/parseus-<version>
    sudo python3 manage_parseus.py upgrade --method bundle

The examples below omit the `cd` and script path for brevity; run them the same way (`sudo python3 manage_parseus.py upgrade ...`) from the new release directory.

Override integrations during upgrade:

    sudo manage-parseus upgrade --integrations gpib,winlog

Keep obsolete images recorded in the previous manifest:

    sudo manage-parseus upgrade --keep-old-images

Open a web browser to Parseus after upgrade completes:

    sudo manage-parseus upgrade --open-browser

Upgrade logs the application version transition recorded in the install manifest, for example `Upgrading Parseus 2026.11.02 -> 2026.12.01`. If both the current and target versions are comparable numeric versions, the command refuses an accidental downgrade. To perform an intentional downgrade, pass:

    sudo manage-parseus upgrade --allow-downgrade

If `--integrations` is omitted during upgrade, the script reuses integrations recorded in the current installation manifest. If no manifest is present, upgrade defaults to no integrations.

By default, upgrade removes obsolete images recorded in the previous installation manifest when they are no longer needed. Use `--keep-old-images` to retain them.

Before starting the upgraded stack, upgrade runs `parseus migrate --noinput`, `parseus populate_analytics`, and `parseus create_periodic_tasks` in one-off `web` containers. This applies Django migrations shipped with the new application image and refreshes installer-managed bootstrap records before the readiness checks complete.

Upgrade removes the `parseus_static_data` Docker volume as part of upgrade cleanup.

## Runtime management

Start an existing installation:

    sudo manage-parseus up

Stop it:

    sudo manage-parseus down

Restart it:

    sudo manage-parseus restart

Show installation and service status:

    sudo manage-parseus status

For `up` and `restart`, `--integrations` may be used to override manifest integrations:

    sudo manage-parseus up --integrations gpib,winlog
    sudo manage-parseus restart --integrations gpib,winlog

If the requested integrations differ from the ones recorded in the manifest, `up` performs a full restart (stop, then start) instead of a plain start. Compose does not stop containers belonging to a profile that is no longer selected, and the systemd unit ignores a plain start while it is already active, so a full restart is the only way to guarantee that the running stack matches the requested integrations, including when an integration is removed. Core services are briefly unavailable during this restart. If the integrations are unchanged (or `--integrations` is omitted), `up` does not restart a running stack.

Open a web browser to Parseus after start or restart:

    sudo manage-parseus up --open-browser
    sudo manage-parseus restart --open-browser

On hosts with systemd, install and upgrade create and enable a `parseus.service` systemd unit, so the stack starts automatically on boot. The `up`, `down`, and `restart` commands drive this unit (`systemctl start`/`stop`) after running their preflight checks and pre-start Django database setup, and refresh the unit's selected integration profiles before starting. On hosts without systemd, these commands fall back to driving Docker Compose directly. You can also operate the unit with the standard tools once installed:

    sudo systemctl status parseus
    sudo systemctl restart parseus
    sudo journalctl -u parseus

The `status` command prints the application bundle directory, installed/partial marker state, public URL, readiness endpoint, manifest metadata, selected integrations, and Docker Compose service status when Docker and the installed Compose files are available. On hosts with systemd, it also prints `Boot-enabled (systemd)` and `Service state (systemd)` from `systemctl is-enabled`/`is-active`. When an installation manifest is available, status also prints `App version: <version>` from the manifest so operators can confirm the deployed Parseus release. `status` can be run with `sudo`, or by a member of the `parseus-admin` group without `sudo`, consistent with the runtime commands. Installed state is based on the managed runtime files and standard installation directories; the manifest is reported separately. If runtime markers are present but the manifest is missing, status reports the installation as present with `Installation state: runtime markers present, manifest missing` so the deployment can be inspected or repaired instead of being mistaken for no installation. It does not start or stop services.

## Access

After install, upgrade, start, or restart, open:

    https://127.0.0.1/

The default deployment uses a self-signed certificate, so your browser may show a certificate warning. If external TLS certificate files are configured, nginx uses the provided certificate and key instead.

Additional endpoints may be available depending on configuration and enabled integrations:

- HTTP-to-HTTPS redirect: `http://127.0.0.1/`
- Elasticsearch: `https://127.0.0.1/elasticsearch/`
- Kibana: `https://127.0.0.1/kibana/`
- Logstash file ingestion (`winlog` integration): host drop directory mounted from `PARSEUS_FTP_HOST_DIR` (default `/srv/parseus/ftp`)

## Uninstallation

Uninstall Parseus:

    sudo manage-parseus uninstall

By default, uninstall removes containers, named Docker volumes, generated environment files, certificates, installation state, `/opt/parseus`, `/etc/parseus`, `/var/lib/parseus`, `/var/log/parseus`, the `manage-parseus` wrapper, the `parseus.service` systemd unit (disabled and removed on hosts with systemd), and Docker images recorded in the manifest. It keeps `/srv/parseus/ftp` unless `--purge-ingest-drop` is provided.

Before uninstalling interactively, the script asks for confirmation. Type `uninstall` to continue.

Keep manifest-recorded images:

    sudo manage-parseus uninstall --keep-images

Skip the interactive confirmation prompt for automation:

    sudo manage-parseus uninstall --yes

Also remove the Winlog ingest drop directory and installer-created system accounts when safe:

    sudo manage-parseus uninstall --purge-ingest-drop --remove-system-account

> Warning: uninstall removes persistent Docker volumes and installer-managed state. Stored application data may be permanently deleted.
>
> Warning: by default, uninstall also removes Docker images recorded in the installation manifest. Use `--keep-images` if those images should be retained.

## Command reference

### Common options

`--config PATH`

: Reads installer settings from an INI-style configuration file. Explicit CLI options override values from this file.

`--integrations gpib,winlog`

: Enables optional components as a comma-separated list. Available integrations are `gpib` and `winlog`.

`--project-name NAME`

: Selects the Docker Compose project/container/volume prefix. Default: `parseus`. Use a unique value when running multiple installations on one Docker host.

### `install`

    sudo python3 manage_parseus.py install [--config PATH] [--method repository|bundle] [--integrations gpib,winlog] [--project-name NAME] [--open-browser]

Default method: `repository`.

### `upgrade`

    sudo manage-parseus upgrade [--config PATH] [--method repository|bundle] [--integrations gpib,winlog] [--project-name NAME] [--keep-old-images] [--allow-downgrade] [--open-browser]

Default method: `repository`.

### `up`

    sudo manage-parseus up [--config PATH] [--integrations gpib,winlog] [--project-name NAME] [--open-browser]

### `down`

    sudo manage-parseus down [--config PATH] [--project-name NAME]

This stops the Docker Compose stack but does not remove volumes, images, manifests, generated files, or installation state.

### `restart`

    sudo manage-parseus restart [--config PATH] [--integrations gpib,winlog] [--project-name NAME] [--open-browser]

### `status`

    sudo manage-parseus status [--config PATH] [--integrations gpib,winlog] [--project-name NAME]

Shows installation, manifest, readiness endpoint, and Docker Compose service status. `--integrations` may be used to choose Compose profiles for service status output; otherwise manifest integrations are used when available.

### `sample-config`

    manage-parseus sample-config > parseus-install.ini

Prints a comprehensive commented installer configuration template to stdout. This command does not require Docker, Linux, or an existing installation.

### `validate-config`

    sudo manage-parseus validate-config [--config PATH] [--method repository|bundle] [--integrations gpib,winlog] [--project-name NAME]

Validates installer configuration, applies supported CLI overrides, prints effective settings, and exits without starting Docker or modifying installed files.

### `uninstall`

    sudo manage-parseus uninstall [--config PATH] [--project-name NAME] [--keep-images] [--yes] [--purge-ingest-drop] [--remove-system-account]

### `create-key`

Generate a Fernet key:

    manage-parseus create-key --kind fernet

Generate a Django secret key:

    manage-parseus create-key --kind django-secret
