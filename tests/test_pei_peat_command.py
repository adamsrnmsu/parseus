"""Characterization tests for build_peat_command."""

from pathlib import Path

import pytest

from parseus.webapp.pei import peat_process


@pytest.fixture(autouse=True)
def fixed_prefix(monkeypatch):
    monkeypatch.setattr(peat_process, "_peat_cli_prefix", lambda: ["peat"])


def build(**kwargs):
    args = {"method": "scan", "targets": ["10.0.0.1"], "run_dir": Path("/r")}
    args.update(kwargs)
    return peat_process.build_peat_command(**args)


def test_minimal_scan_command():
    assert build() == ["peat", "scan", "--no-color", "-d", "all", "--run-dir", "/r", "-i", "10.0.0.1"]


def test_all_options_in_order():
    cmd = build(
        method="scan",
        targets=[" 10.0.0.1 ", "", "10.0.0.2"],
        config_file="/c.yaml",
        elastic_server="http://es",
        intensive_scan=True,
        device_types=["sel", " ", "ab"],
    )
    assert cmd == [
        "peat", "scan", "--no-color", "-d", "sel", "ab", "--run-dir", "/r",
        "-c", "/c.yaml", "-e", "http://es", "--intensive-scan",
        "-i", "10.0.0.1", "10.0.0.2",
    ]


def test_pull_ignores_intensive_flag_and_serial_uses_s():
    cmd = build(method="pull", intensive_scan=True, target_mode="serial", targets=["1"])
    assert "--intensive-scan" not in cmd
    assert cmd[-2:] == ["-s", "1"] and cmd[1] == "pull"


@pytest.mark.parametrize("types", [None, [], [" "], ["sel", "ALL"]])
def test_device_types_default_to_all(types):
    cmd = build(device_types=types)
    assert cmd[cmd.index("-d") + 1 : cmd.index("--run-dir")] == ["all"]


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"method": "x"}, "Unsupported PEAT method"),
        ({"target_mode": "x"}, "Unsupported PEAT target mode"),
        ({"targets": [" ", ""]}, "At least one"),
    ],
)
def test_invalid_inputs_raise(kwargs, message):
    with pytest.raises(ValueError, match=message):
        build(**kwargs)
