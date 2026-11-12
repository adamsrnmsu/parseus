from __future__ import annotations

MANIFEST_SCHEMA_VERSION = 1

DEFAULT_CMD_TIMEOUT = 60
LONG_CMD_TIMEOUT = 60 * 60
DEFAULT_URL = "https://127.0.0.1/"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_HTTPS_PORT = 443
DEFAULT_DOCKER_PROJECT_NAME = "parseus"

UPGRADE_REMOVABLE_VOLUME_SUFFIXES = [
    "_static_data",
]

UPGRADE_REMOVABLE_VOLUMES = [
    "parseus_static_data",
]

UNINSTALL_REMOVABLE_VOLUMES = [
    "parseus_database_data",
    "parseus_datastore_data",
    "parseus_app_log_data",
    "parseus_nginx_log_data",
    "parseus_media_data",
    "parseus_pcap_data",
    "parseus_static_data",
    "parseus_zeek_log_data",
]

INTEGRATION_TO_COMPOSE_PROFILE = {
    "gpib": "gpib",
    "winlog": "winlog",
    "zeek": "zeek",
}
AVAILABLE_INTEGRATIONS = set(INTEGRATION_TO_COMPOSE_PROFILE)

ENV_CONFIG_SECTION_TO_FILE = {
    "env.datastore": "datastore.env",
    "env.production": "production.env",
    "env.ingestor": "ingestor.env",
    "env.ingestor_health": "ingestor_health.env",
}

INSTALLER_CONFIG_SECTION_OPTIONS = {
    "install": {"method", "integrations", "project_name"},
    "nginx": {"http_bind", "http_port", "https_bind", "https_port"},
    "network": {"public_url", "health_host", "https_port"},
    "certificates": {"common_name", "days", "san_dns", "san_ips", "cert_file", "key_file"},
}
ARBITRARY_INSTALLER_CONFIG_SECTIONS = {"compose", *ENV_CONFIG_SECTION_TO_FILE}
SUPPORTED_INSTALLER_CONFIG_SECTIONS = (
    set(INSTALLER_CONFIG_SECTION_OPTIONS) | ARBITRARY_INSTALLER_CONFIG_SECTIONS
)
