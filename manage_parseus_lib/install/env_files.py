from __future__ import annotations

from pathlib import Path
from typing import List, Optional
import os
from urllib.parse import urlparse

from manage_parseus_lib.install.paths import InstallPaths
from manage_parseus_lib.file_ops import atomic_write_text, backup_file_before_overwrite, ensure_directory
from manage_parseus_lib.keys import generate_django_secret_key, generate_fernet_key
from manage_parseus_lib.output import die, ok
from manage_parseus_lib.system.permissions import set_config_file_permissions
from manage_parseus_lib.types import (
    ComposeEnv,
    EnvDefaults,
    EnvFileSpecs,
    EnvFileValues,
    EnvFileWriteValues,
    EnvGenerators,
    EnvOverrides,
)

def ensure_env_dir(install_paths: InstallPaths) -> None:
    ensure_directory(install_paths.env_dir, mode=0o750)

def ensure_managed_env_file(
    path: Path,
    defaults: EnvDefaults,
    generators: Optional[EnvGenerators] = None,
    header: Optional[str] = None,
    preserve_existing: bool = True,
) -> None:
    generators = generators or {}

    current = parse_env_file(path) if preserve_existing else {}
    result = dict(current)

    for key, value in defaults.items():
        if key not in result or result[key] == "":
            result[key] = value

    for key, gen in generators.items():
        if key not in result or result[key] == "":
            result[key] = gen()

    write_env_file(path, result, header=header)

def generate_env_files(
    install_paths: InstallPaths,
    integrations: List[str],
    env_overrides: Optional[EnvOverrides] = None,
    public_url: str = "https://127.0.0.1/",
) -> None:
    specs = get_env_file_specs(public_url=public_url)
    apply_env_overrides_to_specs(specs, env_overrides or {})

    ensure_managed_env_file(
        install_paths.env_dir / "datastore.env",
        defaults=specs["datastore.env"]["defaults"],
        generators=specs["datastore.env"]["generators"],
        header=specs["datastore.env"]["header"],
    )

    ensure_managed_env_file(
        install_paths.env_dir / "production.env",
        defaults=specs["production.env"]["defaults"],
        generators=specs["production.env"]["generators"],
        header=specs["production.env"]["header"],
    )

    ingestor_health_path = install_paths.env_dir / "ingestor_health.env"
    ensure_managed_env_file(
        ingestor_health_path,
        defaults=specs["ingestor_health.env"]["defaults"],
        generators=specs["ingestor_health.env"]["generators"],
        header=specs["ingestor_health.env"]["header"],
    )
    validate_ingestor_health_env_file(ingestor_health_path)

    if "gpib" in integrations or "zeek" in integrations:
        ensure_managed_env_file(
            install_paths.env_dir / "ingestor.env",
            defaults=specs["ingestor.env"]["defaults"],
            generators=specs["ingestor.env"]["generators"],
            header=specs["ingestor.env"]["header"],
        )

def parse_env_file(path: Path) -> EnvFileValues:
    if not path.exists():
        return {}

    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] == '"':
            v = decode_env_value(v[1:-1])
        elif len(v) >= 2 and v[0] == v[-1] == "'":
            v = v[1:-1]
        out[k] = v
    return out


def decode_env_value(value: str) -> str:
    result = []
    escaped = False
    for char in value:
        if escaped:
            if char == "n":
                result.append("\n")
            elif char == "r":
                result.append("\r")
            else:
                result.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            result.append(char)
    if escaped:
        result.append("\\")
    return "".join(result)

def _parse_positive_int_env(env_kv: EnvFileValues, key: str, path: Path) -> int:
    raw = env_kv.get(key, "")
    try:
        value = int(raw)
    except ValueError:
        die(f"Invalid integer for {key} in {path}: {raw!r}")

    if value <= 0:
        die(f"Invalid value for {key} in {path}: {value}. Must be > 0")

    return value

def validate_ingestor_health_env_file(path: Path) -> None:
    env_kv = parse_env_file(path)

    logstash_warn = _parse_positive_int_env(env_kv, "LOGSTASH_WARN_AFTER_SECONDS", path)
    logstash_critical = _parse_positive_int_env(
        env_kv, "LOGSTASH_CRITICAL_AFTER_SECONDS", path
    )
    gpib_warn = _parse_positive_int_env(env_kv, "GPIB_WARN_AFTER_SECONDS", path)
    gpib_critical = _parse_positive_int_env(env_kv, "GPIB_CRITICAL_AFTER_SECONDS", path)

    if logstash_warn >= logstash_critical:
        die(
            "Invalid ingestor health thresholds in "
            f"{path}: LOGSTASH_WARN_AFTER_SECONDS ({logstash_warn}) must be less "
            f"than LOGSTASH_CRITICAL_AFTER_SECONDS ({logstash_critical})"
        )

    if gpib_warn >= gpib_critical:
        die(
            "Invalid ingestor health thresholds in "
            f"{path}: GPIB_WARN_AFTER_SECONDS ({gpib_warn}) must be less than "
            f"GPIB_CRITICAL_AFTER_SECONDS ({gpib_critical})"
        )

def encode_env_value(value: object) -> str:
    s = str(value)
    s = (
        s.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace('"', '\\"')
    )
    return f'"{s}"'

def write_env_file(
    path: Path, kv: EnvFileWriteValues, header: Optional[str] = None
) -> None:
    lines = []

    if header:
        lines.append(f"# {header}")
        lines.append("# This file is installer-managed.")
        lines.append("")

    for k in sorted(kv):
        v = kv[k]
        if v is None:
            continue
        lines.append(f"{k}={encode_env_value(v)}")

    data = "\n".join(lines) + "\n"
    try:
        backup_file_before_overwrite(path, data, mode=0o640)
        atomic_write_text(path, data, mode=0o640)
    except PermissionError as e:
        die(
            f"Cannot write {path}: {e.strerror or e}.\n"
            "This file is root-owned and the change requires root privileges.\n"
            "Re-run with: sudo manage-parseus <command>"
        )
    if getattr(os, "geteuid", lambda: None)() == 0:
        set_config_file_permissions(path, mode=0o640)
    ok(f"Wrote {path}")

def ensure_compose_env_file(install_paths: InstallPaths, compose_env: ComposeEnv) -> None:
    if not compose_env:
        return

    current = parse_env_file(install_paths.compose_env_file)
    result = dict(current)
    result.update(compose_env)

    # Config under /etc/parseus is root-writable only. Skip the rewrite when
    # nothing would change so non-root parseus-admin members can run
    # runtime commands such as `up` and `restart`.
    if install_paths.compose_env_file.exists() and result == current:
        return

    write_env_file(
        install_paths.compose_env_file,
        result,
        header="Parseus Docker Compose configuration",
    )

def apply_env_overrides_to_specs(
    specs: EnvFileSpecs, env_overrides: EnvOverrides
) -> None:
    for env_file, overrides in env_overrides.items():
        if env_file not in specs:
            die(f"Unsupported environment file in installer configuration: {env_file}")

        spec = specs[env_file]
        generators = spec.get("generators", {})
        generated_secret_keys = sorted(set(overrides) & set(generators))
        if generated_secret_keys:
            die(
                "Refusing to set generated secret value(s) from installer configuration: "
                f"{', '.join(generated_secret_keys)}. Generate or rotate secrets with "
                "dedicated commands instead."
            )

        defaults = dict(spec.get("defaults", {}))
        defaults.update(overrides)
        spec["defaults"] = defaults

def django_host_and_origin_defaults(public_url: str) -> tuple[str, str]:
    parsed = urlparse(public_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        die(
            "Invalid public URL for Django host/origin defaults: "
            f"{public_url!r}. Expected an http(s) URL with a hostname."
        )

    origin = f"{parsed.scheme}://{parsed.netloc}"
    if parsed.hostname in {"127.0.0.1", "localhost"}:
        alternate_host = "localhost" if parsed.hostname == "127.0.0.1" else "127.0.0.1"
        alternate_netloc = alternate_host
        if parsed.port:
            alternate_netloc = f"{alternate_host}:{parsed.port}"
        alternate_origin = f"{parsed.scheme}://{alternate_netloc}"
        return f"{parsed.hostname},{alternate_host}", f"{origin},{alternate_origin}"

    return parsed.hostname, origin


def get_env_file_specs(public_url: str = "https://127.0.0.1/") -> EnvFileSpecs:
    django_allowed_hosts, django_csrf_trusted_origins = django_host_and_origin_defaults(
        public_url
    )

    return {
        "datastore.env": {
            "header": "Parseus datastore configuration",
            "defaults": {
                "DATASTORE_HOST_URL": "http://elasticsearch.local:9200",
                "DATASTORE_TYPE": "elasticsearch",
            },
            "generators": {},
        },
        "production.env": {
            "header": "Parseus production application configuration",
            "defaults": {
                "CELERY_BROKER_URL": "redis://redis:6379/0",
                "DJANGO_ALLOWED_HOSTS": django_allowed_hosts,
                "DJANGO_CSRF_TRUSTED_ORIGINS": django_csrf_trusted_origins,
                "DJANGO_CSRF_COOKIE_SECURE": True,
                "DJANGO_DEBUG": False,
                "DJANGO_SESSION_COOKIE_SECURE": True,
            },
            "generators": {
                "DJANGO_ENCRYPTION_KEY": generate_fernet_key,
                "DJANGO_SECRET_KEY": generate_django_secret_key,
            },
        },
        "ingestor.env": {
            "header": "Parseus GPIB ingestor configuration",
            "defaults": {
                "DATASTORE_HOST_URL": "https://localhost/elasticsearch/",
                "INGESTOR_INDEX": "scpi_parsed",
                "INGESTOR_GPIB_IP": "localhost",
                "INGESTOR_GPIB_PORT": "1234",
                "INGESTOR_CAPTURE_DIR": "/home/webapp/.parseus/artifacts/pcaps",
                "ZEEK_IFACE": "eth0",
            },
            "generators": {},
        },
        "ingestor_health.env": {
            "header": "Parseus ingestor health check configuration",
            "defaults": {
                "INGESTOR_HEALTH_REDIS_PREFIX": "health:ingestor",
                "LOGSTASH_HEALTH_INDEX_PATTERN": "winlogbeat-*",
                "LOGSTASH_WARN_AFTER_SECONDS": 600,
                "LOGSTASH_CRITICAL_AFTER_SECONDS": 1200,
                "GPIB_HEALTH_INDEX_PATTERN": "scpi_parsed-*",
                "GPIB_WARN_AFTER_SECONDS": 432000,
                "GPIB_CRITICAL_AFTER_SECONDS": 604800,
            },
            "generators": {},
        },
    }

