"""Behaviour tests for manage_parseus_lib config validation, env parsing and manifest helpers."""

from __future__ import annotations

import configparser
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from manage_parseus_lib.config.loader import (
    apply_compose_nginx_network_defaults,
    parse_config_port,
    validate_installer_config_values,
)
from manage_parseus_lib.errors import ManageParseusError
from manage_parseus_lib.install.env_files import parse_env_file
from manage_parseus_lib.state import manifest as manifest_mod
from manage_parseus_lib.state.manifest import (
    manifests_differ_meaningfully,
    normalize_for_fingerprint,
    stable_config_fingerprint,
    utc_now_iso,
)


def _parser(text: str = "") -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read_string(text)
    return parser


CFG = Path("installer.ini")


# --- apply_compose_nginx_network_defaults -----------------------------------


def _net_values(**compose_env) -> dict:
    return {
        "compose_env": dict(compose_env),
        "health_host": "10.9.9.9",
        "https_port": 9000,
        "public_url": "https://original/",
    }


def test_nginx_defaults_ignore_non_dict_compose_env() -> None:
    values = {"compose_env": "nope", "health_host": "h", "https_port": 1, "public_url": "u"}
    apply_compose_nginx_network_defaults(values, _parser(), CFG)
    assert values == {"compose_env": "nope", "health_host": "h", "https_port": 1, "public_url": "u"}


@pytest.mark.parametrize("compose_env", [{}, {"PARSEUS_HTTPS_BIND": "  ", "PARSEUS_HTTPS_PORT": " "}])
def test_nginx_defaults_noop_without_bind_or_port(compose_env) -> None:
    values = _net_values(**compose_env)
    apply_compose_nginx_network_defaults(values, _parser(), CFG)
    assert values["health_host"] == "10.9.9.9"
    assert values["https_port"] == 9000
    assert values["public_url"] == "https://original/"


def test_nginx_defaults_specific_bind_without_port_uses_current_port() -> None:
    values = _net_values(PARSEUS_HTTPS_BIND=" 192.0.2.7 ")
    apply_compose_nginx_network_defaults(values, _parser(), CFG)
    assert values["health_host"] == "192.0.2.7"
    assert values["https_port"] == 9000
    assert values["public_url"] == "https://192.0.2.7:9000/"


@pytest.mark.parametrize("bind", ["0.0.0.0", "::"])
def test_nginx_defaults_wildcard_bind_uses_loopback_health_host(bind) -> None:
    values = _net_values(PARSEUS_HTTPS_BIND=bind, PARSEUS_HTTPS_PORT="443")
    apply_compose_nginx_network_defaults(values, _parser(), CFG)
    assert values["health_host"] == "127.0.0.1"
    assert values["https_port"] == 443
    assert values["public_url"] == "https://127.0.0.1/"


def test_nginx_defaults_port_only_keeps_health_host() -> None:
    values = _net_values(PARSEUS_HTTPS_PORT=" 8443 ")
    apply_compose_nginx_network_defaults(values, _parser(), CFG)
    assert values["health_host"] == "10.9.9.9"
    assert values["https_port"] == 8443
    assert isinstance(values["https_port"], int)
    assert values["public_url"] == "https://10.9.9.9:8443/"


@pytest.mark.parametrize("port", ["1", "65535"])
def test_nginx_defaults_accept_port_range_edges(port) -> None:
    values = _net_values(PARSEUS_HTTPS_PORT=port)
    apply_compose_nginx_network_defaults(values, _parser(), CFG)
    assert values["https_port"] == int(port)


@pytest.mark.parametrize("port", ["0", "65536", "-1", "abc", "44.3"])
def test_nginx_defaults_reject_bad_port(port) -> None:
    values = _net_values(PARSEUS_HTTPS_PORT=port)
    with pytest.raises(ManageParseusError, match="Invalid Compose HTTPS port") as exc:
        apply_compose_nginx_network_defaults(values, _parser(), CFG)
    assert repr(port) in exc.value.message or port in exc.value.message
    assert str(CFG) in exc.value.message
    assert values["https_port"] == 9000


@pytest.mark.parametrize(
    "option,key,expected",
    [
        ("health_host", "health_host", "10.9.9.9"),
        ("https_port", "https_port", 9000),
        ("public_url", "public_url", "https://original/"),
    ],
)
def test_nginx_defaults_keep_explicit_network_options(option, key, expected) -> None:
    parser = _parser(f"[network]\n{option} = whatever\n")
    values = _net_values(PARSEUS_HTTPS_BIND="192.0.2.7", PARSEUS_HTTPS_PORT="8443")
    apply_compose_nginx_network_defaults(values, parser, CFG)
    assert values[key] == expected
    others = {"health_host": "192.0.2.7", "https_port": 8443, "public_url": "https://192.0.2.7:8443/"}
    for other, derived in others.items():
        if other != key:
            assert values[other] == derived


def test_nginx_defaults_do_not_touch_compose_env() -> None:
    values = _net_values(PARSEUS_HTTPS_BIND="192.0.2.7", PARSEUS_HTTPS_PORT="8443")
    apply_compose_nginx_network_defaults(values, _parser(), CFG)
    assert values["compose_env"] == {"PARSEUS_HTTPS_BIND": "192.0.2.7", "PARSEUS_HTTPS_PORT": "8443"}


# --- parse_config_port ------------------------------------------------------


def test_parse_config_port_default_when_missing() -> None:
    assert parse_config_port(_parser("[nginx]\n"), "nginx", "http_port", CFG, default=80) == 80


@pytest.mark.parametrize("port", [1, 80, 65535])
def test_parse_config_port_accepts_valid(port) -> None:
    parser = _parser(f"[nginx]\nhttp_port = {port}\n")
    assert parse_config_port(parser, "nginx", "http_port", CFG, default=1234) == port


@pytest.mark.parametrize("port", ["0", "-5", "65536", "100000"])
def test_parse_config_port_rejects_out_of_range(port) -> None:
    parser = _parser(f"[nginx]\nhttp_port = {port}\n")
    with pytest.raises(ManageParseusError) as exc:
        parse_config_port(parser, "nginx", "http_port", CFG, default=80)
    assert f"Invalid port for [nginx] http_port in {CFG}: {port}" == exc.value.message


def test_parse_config_port_rejects_non_integer() -> None:
    parser = _parser("[nginx]\nhttp_port = eighty\n")
    with pytest.raises(ManageParseusError, match="Invalid integer for"):
        parse_config_port(parser, "nginx", "http_port", CFG, default=80)


# --- validate_installer_config_values --------------------------------------


def _valid_values(**over) -> dict:
    values = {
        "method": "repository",
        "integrations_arg": None,
        "project_name": "parseus",
        "public_url": "https://127.0.0.1/",
        "health_host": "127.0.0.1",
        "https_port": 443,
        "certificate_common_name": "localhost",
        "certificate_days": 825,
        "certificate_san_dns": ["localhost"],
        "certificate_san_ips": ["127.0.0.1"],
        "certificate_cert_file": None,
        "certificate_key_file": None,
        "env_overrides": {},
        "compose_env": {},
    }
    values.update(over)
    return values


def _rejects(match: str, **over) -> None:
    with pytest.raises(ManageParseusError, match=match):
        validate_installer_config_values(_valid_values(**over))


def test_validate_accepts_baseline() -> None:
    validate_installer_config_values(_valid_values())


@pytest.mark.parametrize("method", ["bundle", "repository"])
def test_validate_accepts_known_methods(method) -> None:
    validate_installer_config_values(_valid_values(method=method))


@pytest.mark.parametrize("method", ["docker", "", "Bundle"])
def test_validate_rejects_unknown_method(method) -> None:
    _rejects("Invalid install method", method=method)


@pytest.mark.parametrize("name", ["a", "0", "a_b-c", "a" * 63])
def test_validate_accepts_project_names(name) -> None:
    validate_installer_config_values(_valid_values(project_name=name))


@pytest.mark.parametrize("name", ["", "a" * 64, "_a", "-a", "A", "a.b", "a b", None, 5])
def test_validate_rejects_project_names(name) -> None:
    _rejects("Invalid Docker Compose project name", project_name=name)


def test_validate_checks_integrations_only_when_given() -> None:
    validate_installer_config_values(_valid_values(integrations_arg="gpib"))
    validate_installer_config_values(_valid_values(integrations_arg=""))
    _rejects("Unknown option.*installer configuration", integrations_arg="no-such-integration")


@pytest.mark.parametrize("port", [1, 65535])
def test_validate_accepts_https_port_edges(port) -> None:
    validate_installer_config_values(_valid_values(https_port=port))


@pytest.mark.parametrize("port", [0, -1, 65536, "443", None, 443.0])
def test_validate_rejects_bad_https_port(port) -> None:
    _rejects("Invalid HTTPS port", https_port=port)


@pytest.mark.parametrize("days", [1, 825])
def test_validate_accepts_certificate_days(days) -> None:
    validate_installer_config_values(_valid_values(certificate_days=days))


@pytest.mark.parametrize("days", [0, -1, "30", None, 1.5])
def test_validate_rejects_bad_certificate_days(days) -> None:
    _rejects("Invalid certificate lifetime", certificate_days=days)


@pytest.mark.parametrize("name", ["", "   ", "a\nb", "a\x7fb", "a\x1fb", None])
def test_validate_rejects_bad_common_name(name) -> None:
    _rejects("common_name", certificate_common_name=name)


def test_validate_accepts_unusual_but_printable_common_name() -> None:
    validate_installer_config_values(_valid_values(certificate_common_name="a b\x20c"))


def test_validate_san_requirements() -> None:
    validate_installer_config_values(_valid_values(certificate_san_dns=["a.example"], certificate_san_ips=[]))
    validate_installer_config_values(_valid_values(certificate_san_dns=[], certificate_san_ips=["::1"]))
    _rejects("at least one", certificate_san_dns=[], certificate_san_ips=[])
    _rejects("SAN configuration", certificate_san_dns="a.example")
    _rejects("SAN configuration", certificate_san_ips="127.0.0.1")
    _rejects("SAN configuration", certificate_san_dns=None)
    _rejects("san_dns", certificate_san_dns=["ok", " "])
    _rejects("san_ips", certificate_san_ips=["127.0.0.1", "nope"])
    _rejects("san_ips entry: must not be empty", certificate_san_ips=[""])
    _rejects("san_ips entry: must not be empty", certificate_san_ips=["  "])
    _rejects("san_ips entry: must not be empty", certificate_san_ips=[None])
    _rejects("san_dns: must not be empty", certificate_san_dns=[None])


def test_validate_certificate_files(tmp_path: Path) -> None:
    cert = tmp_path / "c.pem"
    key = tmp_path / "k.pem"
    cert.write_text("cert")
    key.write_text("key")

    validate_installer_config_values(
        _valid_values(certificate_cert_file=cert, certificate_key_file=key)
    )
    _rejects("provided together", certificate_cert_file=cert)
    _rejects("provided together", certificate_key_file=key)
    _rejects("cert_file does not exist", certificate_cert_file=tmp_path / "missing", certificate_key_file=key)
    _rejects("key_file does not exist", certificate_cert_file=cert, certificate_key_file=tmp_path)
    _rejects("cert_file", certificate_cert_file=str(cert), certificate_key_file=key)


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="needs non-root POSIX")
def test_validate_rejects_unreadable_certificate_file(tmp_path: Path) -> None:
    cert = tmp_path / "c.pem"
    key = tmp_path / "k.pem"
    cert.write_text("cert")
    key.write_text("key")
    key.chmod(0)
    try:
        _rejects("key_file is not readable", certificate_cert_file=cert, certificate_key_file=key)
    finally:
        key.chmod(0o600)


@pytest.mark.parametrize("key", ["public_url", "health_host", "certificate_common_name"])
@pytest.mark.parametrize("bad", ["", "  ", None])
def test_validate_rejects_empty_required_strings(key, bad) -> None:
    with pytest.raises(ManageParseusError):
        validate_installer_config_values(_valid_values(**{key: bad}))


@pytest.mark.parametrize("key", ["public_url", "health_host"])
def test_validate_empty_url_or_host_message(key) -> None:
    _rejects(f"value for {key}", **{key: " "})


# --- parse_env_file ---------------------------------------------------------


def test_parse_env_file_missing_file_is_empty(tmp_path: Path) -> None:
    assert parse_env_file(tmp_path / "absent.env") == {}


def test_parse_env_file_parses_quotes_escapes_and_skips_noise(tmp_path: Path) -> None:
    path = tmp_path / "x.env"
    path.write_text(
        "\n".join(
            [
                "# comment=ignored",
                "   # indented=comment",
                "",
                "   ",
                "no_equals_here",
                "PLAIN=value",
                "  SPACED  =  padded value  ",
                'DQ="two words"',
                "SQ='single quoted'",
                'ESC="line1\\nline2\\r\\"q\\" back\\\\slash"',
                "SQ_ESC='a\\nb'",
                "EMPTY=",
                'EMPTY_DQ=""',
                "EMPTY_SQ=''",
                'LONE_DQ="',
                "LONE_SQ='",
                "MIXED=\"abc'",
                "MIXED2='abc\"",
                "EQ=a=b=c",
                'DQ_INNER="\'x\'"',
                "SQ_INNER='\"x\"'",
                "TWO=1",
                "TWO=2",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert parse_env_file(path) == {
        "PLAIN": "value",
        "SPACED": "padded value",
        "DQ": "two words",
        "SQ": "single quoted",
        "ESC": 'line1\nline2\r"q" back\\slash',
        "SQ_ESC": "a\\nb",
        "EMPTY": "",
        "EMPTY_DQ": "",
        "EMPTY_SQ": "",
        "LONE_DQ": '"',
        "LONE_SQ": "'",
        "MIXED": "\"abc'",
        "MIXED2": "'abc\"",
        "EQ": "a=b=c",
        "DQ_INNER": "'x'",
        "SQ_INNER": '"x"',
        "TWO": "2",
    }


def test_parse_env_file_handles_value_that_is_only_quotes(tmp_path: Path) -> None:
    path = tmp_path / "x.env"
    path.write_text('A=""\nB=\'\'\nC=""x""\nD="a"\nE=\'a\'\n', encoding="utf-8")
    assert parse_env_file(path) == {"A": "", "B": "", "C": '"x"', "D": "a", "E": "a"}


# --- manifests_differ_meaningfully -----------------------------------------


def _manifest(**over) -> dict:
    manifest = {
        "schema_version": 1,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "app_version": "1.0.0",
        "integrations": ["gpib"],
        "all_images": ["a:1", "b:1"],
        "app_owned_images": ["a:1"],
        "dependency_images": ["b:1"],
        "resolved_images": {"a:1": "sha256:aa"},
        "named_volumes": ["vol"],
        "installer_config": {"project_name": "parseus"},
    }
    manifest.update(over)
    return manifest


def test_manifests_differ_when_no_previous_manifest() -> None:
    assert manifests_differ_meaningfully(None, _manifest()) is True


def test_manifests_equal_copy_does_not_differ() -> None:
    assert manifests_differ_meaningfully(_manifest(), _manifest()) is False


@pytest.mark.parametrize(
    "key,new_value",
    [
        ("schema_version", 2),
        ("app_version", "2.0.0"),
        ("integrations", ["winlog"]),
        ("all_images", ["a:2"]),
        ("app_owned_images", ["a:2"]),
        ("dependency_images", ["b:2"]),
        ("resolved_images", {"a:1": "sha256:bb"}),
        ("named_volumes", ["other"]),
        ("installer_config", {"project_name": "other"}),
    ],
)
def test_manifests_differ_when_a_compared_field_changes(key, new_value) -> None:
    assert manifests_differ_meaningfully(_manifest(), _manifest(**{key: new_value})) is True


def test_manifests_differ_when_compared_field_is_added_or_removed() -> None:
    old = _manifest()
    del old["named_volumes"]
    assert manifests_differ_meaningfully(old, _manifest()) is True
    assert manifests_differ_meaningfully(_manifest(), old) is True


def test_manifests_ignore_install_timestamp_and_system_accounts() -> None:
    new = _manifest(installed_at="2030-01-01T00:00:00+00:00", system_accounts={"uid": 1})
    assert manifests_differ_meaningfully(_manifest(), new) is False


# --- stable_config_fingerprint / normalize_for_fingerprint ------------------


def test_fingerprint_matches_known_canonical_json_digest() -> None:
    assert (
        stable_config_fingerprint({"b": 1, "a": [1, 2]})
        == hashlib.sha256(b'{"a":[1,2],"b":1}').hexdigest()
        == "94a786c3662bc7beeb598efa7d8cb58d7bea25d6c275ea9785a0230ff1f8c2ba"
    )


def test_fingerprint_ignores_key_order_but_not_values() -> None:
    assert stable_config_fingerprint({"a": 1, "b": 2}) == stable_config_fingerprint({"b": 2, "a": 1})
    assert stable_config_fingerprint({"a": 1}) != stable_config_fingerprint({"a": 2})
    assert stable_config_fingerprint([1, 2]) != stable_config_fingerprint([2, 1])


def test_fingerprint_is_hex_sha256_and_treats_tuple_like_list() -> None:
    digest = stable_config_fingerprint({"k": ("x", "y")})
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    assert digest == stable_config_fingerprint({"k": ["x", "y"]})


def test_normalize_passes_json_scalars_through_unchanged() -> None:
    assert normalize_for_fingerprint(None) is None
    assert normalize_for_fingerprint(True) is True
    assert normalize_for_fingerprint(False) is False
    assert normalize_for_fingerprint(7) == 7 and isinstance(normalize_for_fingerprint(7), int)
    assert normalize_for_fingerprint(1.5) == 1.5
    assert normalize_for_fingerprint("s") == "s"


def test_normalize_recurses_and_stringifies_unknown_types() -> None:
    value = {1: (Path("/a"), [None, {"x": ("t",)}]), "s": {2, }}
    assert normalize_for_fingerprint(value) == {
        "1": ["/a" if os.sep == "/" else str(Path("/a")), [None, {"x": ["t"]}]],
        "s": "{2}",
    }


# --- utc_now_iso ------------------------------------------------------------


def test_utc_now_iso_is_utc_second_precision(monkeypatch) -> None:
    seen = {}

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            seen["tz"] = tz
            return cls(2026, 1, 2, 3, 4, 5, 678901, tzinfo=tz)

    monkeypatch.setattr(manifest_mod, "datetime", FakeDatetime)
    assert utc_now_iso() == "2026-01-02T03:04:05+00:00"
    assert seen["tz"] is timezone.utc


def test_utc_now_iso_real_clock_round_trips() -> None:
    before = datetime.now(timezone.utc).replace(microsecond=0)
    parsed = datetime.fromisoformat(utc_now_iso())
    after = datetime.now(timezone.utc)
    assert parsed.utcoffset().total_seconds() == 0
    assert parsed.microsecond == 0
    assert before <= parsed <= after
