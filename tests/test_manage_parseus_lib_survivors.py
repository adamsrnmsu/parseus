"""Behavioural tests for manage_parseus_lib config loader, env-file and manifest helpers."""

from __future__ import annotations

import argparse
import configparser
import json
from pathlib import Path

import pytest

from manage_parseus_lib.config.loader import build_installer_config, parse_config_str
from manage_parseus_lib.errors import ManageParseusError
from manage_parseus_lib.install import env_files
from manage_parseus_lib.install.env_files import (
    django_host_and_origin_defaults,
    ensure_compose_env_file,
    ensure_managed_env_file,
    generate_env_files,
    parse_env_file,
    validate_ingestor_health_env_file,
)
from manage_parseus_lib.install.paths import InstallPaths, build_paths
from manage_parseus_lib.state import manifest as manifest_mod
from manage_parseus_lib.state.manifest import (
    app_version_from_image_ref,
    archive_current_manifest,
    purge_manifest_images,
    remove_obsolete_manifest_images,
    save_current_install_manifest,
)


def _paths(tmp_path: Path) -> InstallPaths:
    base = build_paths(source_root=tmp_path / "source")
    return type(base)(
        source_root=base.source_root,
        app_root=tmp_path / "opt" / "parseus",
        config_root=tmp_path / "etc" / "parseus",
        data_root=tmp_path / "var" / "lib" / "parseus",
        state_root=tmp_path / "var" / "lib" / "parseus" / "state",
        runtime_root=tmp_path / "var" / "lib" / "parseus" / "runtime",
        drop_dir=tmp_path / "srv" / "parseus" / "ftp",
        log_root=tmp_path / "var" / "log" / "parseus",
        wrapper_path=tmp_path / "usr" / "local" / "bin" / "manage-parseus",
        project_name=base.project_name,
    )


@pytest.fixture(autouse=True)
def _no_chown(monkeypatch):
    monkeypatch.setattr(manifest_mod, "set_state_file_permissions", lambda *a, **k: None)
    monkeypatch.setattr(env_files, "set_config_file_permissions", lambda *a, **k: None)


def _args(config: Path) -> argparse.Namespace:
    return argparse.Namespace(config=config, method=None, integrations=None, project_name=None)


# --- config/loader.py ---------------------------------------------------------


def test_config_path_that_is_a_directory_reports_not_found(tmp_path: Path) -> None:
    with pytest.raises(ManageParseusError, match="not found"):
        build_installer_config(_args(tmp_path))


def test_config_path_missing_reports_not_found(tmp_path: Path) -> None:
    with pytest.raises(ManageParseusError, match="not found"):
        build_installer_config(_args(tmp_path / "nope.ini"))


def test_parse_config_str_rejects_blank_value_and_strips_others() -> None:
    parser = configparser.ConfigParser()
    parser.read_string("[nginx]\nblank =    \nbind =  10.0.0.1  \n")
    with pytest.raises(ManageParseusError, match="empty value"):
        parse_config_str(parser, "nginx", "blank", Path("x.ini"))
    assert parse_config_str(parser, "nginx", "bind", Path("x.ini")) == "10.0.0.1"


def test_parse_config_str_missing_option_default_or_error() -> None:
    parser = configparser.ConfigParser()
    parser.read_string("[nginx]\n")
    assert parse_config_str(parser, "nginx", "bind", Path("x"), default="1.2.3.4") == "1.2.3.4"
    with pytest.raises(ManageParseusError, match="Missing required"):
        parse_config_str(parser, "nginx", "bind", Path("x"))


def test_unreadable_certificate_file_is_reported(tmp_path: Path, monkeypatch) -> None:
    cert = tmp_path / "c.pem"
    key = tmp_path / "k.pem"
    cert.write_text("c")
    key.write_text("k")
    cfg = tmp_path / "i.ini"
    cfg.write_text(f"[certificates]\ncert_file = {cert}\nkey_file = {key}\n")

    # Readable files are accepted.
    assert build_installer_config(_args(cfg)).certificate_cert_file == cert

    real_open = Path.open

    class _Broken:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            if n == 0:
                return b""
            raise OSError("input/output error")

    def fake_open(self, mode="r", *a, **k):
        if self == cert and "b" in mode:
            return _Broken()
        return real_open(self, mode, *a, **k)

    monkeypatch.setattr(Path, "open", fake_open)
    with pytest.raises(ManageParseusError, match="not readable"):
        build_installer_config(_args(cfg))


# --- install/env_files.py -----------------------------------------------------


def test_ensure_managed_env_file_fills_missing_and_empty_keeps_set(tmp_path: Path) -> None:
    path = tmp_path / "a.env"
    path.write_text('KEEP="mine"\nEMPTY=""\nGEN_EMPTY=""\nGEN_KEEP="g"\nEXTRA="x"\n')
    ensure_managed_env_file(
        path,
        defaults={"KEEP": "default", "EMPTY": "filled", "NEW": "new"},
        generators={"GEN_EMPTY": lambda: "made", "GEN_KEEP": lambda: "other", "GEN_NEW": lambda: "n"},
    )
    assert parse_env_file(path) == {
        "KEEP": "mine",
        "EMPTY": "filled",
        "NEW": "new",
        "GEN_EMPTY": "made",
        "GEN_KEEP": "g",
        "GEN_NEW": "n",
        "EXTRA": "x",
    }


def test_ensure_managed_env_file_without_generators_or_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "a.env"
    ensure_managed_env_file(path, defaults={"A": 1}, header="Hello")
    text = path.read_text()
    assert text.startswith("# Hello\n# This file is installer-managed.\n\n")
    assert parse_env_file(path) == {"A": "1"}


def test_ensure_managed_env_file_can_discard_existing(tmp_path: Path) -> None:
    path = tmp_path / "a.env"
    path.write_text('OLD="1"\n')
    ensure_managed_env_file(path, defaults={"A": "b"}, preserve_existing=False)
    assert parse_env_file(path) == {"A": "b"}


def _generate(tmp_path: Path, integrations, overrides=None) -> InstallPaths:
    paths = _paths(tmp_path)
    generate_env_files(paths, integrations, env_overrides=overrides, public_url="https://example.test/")
    return paths


def test_generate_env_files_writes_core_files_with_secrets(tmp_path: Path) -> None:
    paths = _generate(tmp_path, [])
    names = sorted(p.name for p in paths.env_dir.iterdir() if p.suffix == ".env")
    assert names == ["datastore.env", "ingestor_health.env", "production.env"]

    prod = parse_env_file(paths.env_dir / "production.env")
    assert prod["DJANGO_ALLOWED_HOSTS"] == "example.test"
    assert prod["DJANGO_CSRF_TRUSTED_ORIGINS"] == "https://example.test"
    assert prod["DJANGO_SECRET_KEY"] and prod["DJANGO_ENCRYPTION_KEY"]
    assert parse_env_file(paths.env_dir / "datastore.env")["DATASTORE_TYPE"] == "elasticsearch"
    health = parse_env_file(paths.env_dir / "ingestor_health.env")
    assert health["LOGSTASH_WARN_AFTER_SECONDS"] == "600"
    assert health["GPIB_CRITICAL_AFTER_SECONDS"] == "604800"


def test_generate_env_files_gpib_adds_ingestor_env_and_applies_overrides(tmp_path: Path) -> None:
    paths = _generate(tmp_path, ["gpib"], {"ingestor.env": {"INGESTOR_GPIB_PORT": "9999"}})
    ingestor = parse_env_file(paths.env_dir / "ingestor.env")
    assert ingestor["INGESTOR_GPIB_PORT"] == "9999"
    assert ingestor["INGESTOR_INDEX"] == "scpi_parsed"


def test_generate_env_files_preserves_generated_secrets_on_rerun(tmp_path: Path) -> None:
    paths = _generate(tmp_path, [])
    first = parse_env_file(paths.env_dir / "production.env")
    generate_env_files(paths, [], public_url="https://example.test/")
    assert parse_env_file(paths.env_dir / "production.env") == first


def _health_file(tmp_path: Path, **overrides: object) -> Path:
    values = {
        "LOGSTASH_WARN_AFTER_SECONDS": 1,
        "LOGSTASH_CRITICAL_AFTER_SECONDS": 2,
        "GPIB_WARN_AFTER_SECONDS": 1,
        "GPIB_CRITICAL_AFTER_SECONDS": 2,
    }
    values.update(overrides)
    path = tmp_path / "h.env"
    path.write_text("".join(f'{k}="{v}"\n' for k, v in values.items()))
    return path


def test_health_env_accepts_minimal_positive_values(tmp_path: Path) -> None:
    validate_ingestor_health_env_file(_health_file(tmp_path))


@pytest.mark.parametrize("bad", ["0", "-5"])
@pytest.mark.parametrize(
    "key",
    [
        "LOGSTASH_WARN_AFTER_SECONDS",
        "LOGSTASH_CRITICAL_AFTER_SECONDS",
        "GPIB_WARN_AFTER_SECONDS",
        "GPIB_CRITICAL_AFTER_SECONDS",
    ],
)
def test_health_env_rejects_non_positive(tmp_path: Path, key: str, bad: str) -> None:
    with pytest.raises(ManageParseusError, match="Must be > 0"):
        validate_ingestor_health_env_file(_health_file(tmp_path, **{key: bad}))


@pytest.mark.parametrize("bad", ["abc", ""])
def test_health_env_rejects_non_integer(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ManageParseusError, match="Invalid integer"):
        validate_ingestor_health_env_file(_health_file(tmp_path, GPIB_WARN_AFTER_SECONDS=bad))


@pytest.mark.parametrize("critical", [5, 4])
def test_health_env_logstash_warn_must_be_below_critical(tmp_path: Path, critical: int) -> None:
    with pytest.raises(ManageParseusError, match="LOGSTASH_WARN_AFTER_SECONDS"):
        validate_ingestor_health_env_file(
            _health_file(
                tmp_path, LOGSTASH_WARN_AFTER_SECONDS=5, LOGSTASH_CRITICAL_AFTER_SECONDS=critical
            )
        )


@pytest.mark.parametrize("critical", [5, 4])
def test_health_env_gpib_warn_must_be_below_critical(tmp_path: Path, critical: int) -> None:
    with pytest.raises(ManageParseusError, match="GPIB_WARN_AFTER_SECONDS"):
        validate_ingestor_health_env_file(
            _health_file(tmp_path, GPIB_WARN_AFTER_SECONDS=5, GPIB_CRITICAL_AFTER_SECONDS=critical)
        )


def test_ensure_compose_env_file_empty_input_writes_nothing(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    ensure_compose_env_file(paths, {})
    assert not paths.compose_env_file.exists()


def test_ensure_compose_env_file_creates_and_merges(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    ensure_compose_env_file(paths, {"A": "1"})
    assert parse_env_file(paths.compose_env_file) == {"A": "1"}
    assert paths.compose_env_file.read_text().startswith("# Parseus Docker Compose configuration")

    ensure_compose_env_file(paths, {"B": "2", "A": "3"})
    assert parse_env_file(paths.compose_env_file) == {"A": "3", "B": "2"}
    assert (paths.config_root / "compose.env").is_file()


@pytest.mark.parametrize("url", ["ftp://host/", "http://", "host.example", "https:///path"])
def test_django_host_defaults_reject_bad_urls(url: str) -> None:
    with pytest.raises(ManageParseusError, match="Invalid public URL"):
        django_host_and_origin_defaults(url)


def test_django_host_defaults_for_loopback_and_named_hosts() -> None:
    assert django_host_and_origin_defaults("https://127.0.0.1:8443/") == (
        "127.0.0.1,localhost",
        "https://127.0.0.1:8443,https://localhost:8443",
    )
    assert django_host_and_origin_defaults("http://localhost/") == (
        "localhost,127.0.0.1",
        "http://localhost,http://127.0.0.1",
    )
    assert django_host_and_origin_defaults("https://parseus.example:9000/x") == (
        "parseus.example",
        "https://parseus.example:9000",
    )


# --- state/manifest.py --------------------------------------------------------


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("parseus:1.2.3", "1.2.3"),
        ("parseus:  ", ""),
        ("registry.local:5000/parseus", ""),
        ("registry.local:5000/parseus:2.0", "2.0"),
        (":1.0", ""),
        ("parseus", ""),
        ("reg/parseus", ""),
        ("parseus:1.2.3@sha256:abc", "1.2.3"),
    ],
)
def test_app_version_from_image_ref_cases(ref: str, expected: str) -> None:
    assert app_version_from_image_ref(ref) == expected


def _manifest(**extra) -> dict:
    return {"schema_version": 1, **extra}


def _history(paths: InstallPaths) -> list[Path]:
    return sorted(paths.history_dir.glob("manifest-*.json")) if paths.history_dir.exists() else []


def test_save_manifest_first_time_writes_file_without_archive(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    save_current_install_manifest(paths, _manifest(b=2, a=1))

    raw = paths.current_manifest.read_text()
    assert raw.endswith("}\n")
    assert raw == json.dumps({"a": 1, "b": 2, "schema_version": 1}, indent=2) + "\n"
    assert _history(paths) == []


def test_save_manifest_archives_previous_and_replaces_current(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    save_current_install_manifest(paths, _manifest(v="old"))
    save_current_install_manifest(paths, _manifest(v="new"))

    assert json.loads(paths.current_manifest.read_text())["v"] == "new"
    archived = _history(paths)
    assert len(archived) == 1
    assert archived[0].parent == paths.history_dir
    assert archived[0].name.startswith("manifest-") and archived[0].name.endswith("Z.json")
    assert json.loads(archived[0].read_text())["v"] == "old"


def test_archive_without_current_manifest_is_a_noop(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    archive_current_manifest(paths)
    assert not paths.state_dir.exists()


def test_archive_name_collision_gets_unique_suffix(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime, timezone

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2030, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)

    monkeypatch.setattr(manifest_mod, "datetime", _Frozen)
    paths = _paths(tmp_path)
    paths.state_dir.mkdir(parents=True)
    paths.current_manifest.write_text(json.dumps(_manifest(v=1)))

    archive_current_manifest(paths)
    archive_current_manifest(paths)

    names = sorted(p.name for p in _history(paths))
    assert len(names) == 2
    assert "manifest-20300102T030405.678901Z.json" in names
    other = [n for n in names if n != "manifest-20300102T030405.678901Z.json"][0]
    assert other.startswith("manifest-20300102T030405.678901Z-") and other.endswith(".json")


def test_remove_obsolete_images_removes_only_ids_absent_from_new(monkeypatch) -> None:
    removed: list = []
    monkeypatch.setattr(manifest_mod, "remove_images", lambda ids: removed.append(ids))
    old = _manifest(resolved_images={"a": "id1", "b": "id2", "c": "", "d": "id4"})
    new = _manifest(resolved_images={"a": "id1", "b": "id9"})
    remove_obsolete_manifest_images(old, new)
    assert removed == [["id2", "id4"]]


def test_remove_obsolete_images_noops(monkeypatch) -> None:
    removed: list = []
    monkeypatch.setattr(manifest_mod, "remove_images", lambda ids: removed.append(ids))
    remove_obsolete_manifest_images(None, _manifest(resolved_images={"a": "x"}))
    same = _manifest(resolved_images={"a": "x"})
    remove_obsolete_manifest_images(same, same)
    # An empty-but-present old manifest must also be handled quietly.
    remove_obsolete_manifest_images(_manifest(), same)
    assert removed == []


def test_purge_manifest_images(monkeypatch) -> None:
    removed: list = []
    monkeypatch.setattr(manifest_mod, "remove_images", lambda ids: removed.append(ids))
    purge_manifest_images(None)
    purge_manifest_images(_manifest(resolved_images={"a": "", "b": ""}))
    assert removed == []
    purge_manifest_images(_manifest(resolved_images={"a": "z", "b": "y", "c": "z", "d": ""}))
    assert removed == [["y", "z"]]


def test_write_env_file_permission_error_message_includes_reason(tmp_path: Path, monkeypatch) -> None:
    def deny(*a, **k):
        raise PermissionError("no way")

    monkeypatch.setattr(env_files, "atomic_write_text", deny)
    with pytest.raises(ManageParseusError, match="no way") as exc:
        env_files.write_env_file(tmp_path / "x.env", {"A": "1"})
    assert "sudo manage-parseus" in str(exc.value)

    def deny_errno(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(env_files, "atomic_write_text", deny_errno)
    with pytest.raises(ManageParseusError, match="Permission denied"):
        env_files.write_env_file(tmp_path / "x.env", {"A": "1"})


def test_parse_env_file_quoting_and_skipped_lines(tmp_path: Path) -> None:
    path = tmp_path / "q.env"
    path.write_text(
        "# comment\n\nnoequals\n"
        'DQ="a\\nb"\nSQ=\'single\'\nBARE=plain\nQ1="\nQ2=\'\nEMPTYQ=""\nEQ=a=b\n'
    )
    assert parse_env_file(path) == {
        "DQ": "a\nb",
        "SQ": "single",
        "BARE": "plain",
        "Q1": '"',
        "Q2": "'",
        "EMPTYQ": "",
        "EQ": "a=b",
    }
    assert parse_env_file(tmp_path / "missing.env") == {}


def test_fingerprint_is_stable_and_order_independent() -> None:
    from manage_parseus_lib.state.manifest import stable_config_fingerprint

    a = stable_config_fingerprint({"x": 1, "y": [True, None, ("t", 1.5)]})
    assert a == stable_config_fingerprint({"y": [True, None, ["t", 1.5]], "x": 1})
    assert a != stable_config_fingerprint({"x": 1, "y": [False, None, ["t", 1.5]]})
    assert stable_config_fingerprint({"p": Path("/a")}) == stable_config_fingerprint({"p": "/a"})


def test_manifests_differ_meaningfully() -> None:
    from manage_parseus_lib.state.manifest import manifests_differ_meaningfully

    base = _manifest(app_version="1", integrations=["a"], installed_at="t1")
    assert manifests_differ_meaningfully(None, base) is True
    assert manifests_differ_meaningfully(base, dict(base, installed_at="t2")) is False
    assert manifests_differ_meaningfully(base, dict(base, app_version="2")) is True
    assert manifests_differ_meaningfully(base, dict(base, integrations=["b"])) is True
    no_integrations = _manifest(app_version="1", installed_at="t1")
    assert manifests_differ_meaningfully(no_integrations, dict(no_integrations, integrations=[])) is False


def test_utc_now_iso_has_no_microseconds() -> None:
    from datetime import datetime

    from manage_parseus_lib.state.manifest import utc_now_iso

    parsed = datetime.fromisoformat(utc_now_iso())
    assert parsed.microsecond == 0 and parsed.utcoffset().total_seconds() == 0
