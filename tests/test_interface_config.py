from types import SimpleNamespace

import pytest

from parseus.analytics.registry import register_spec
import parseus.analytics.registry as registry_module
from parseus.analytics.spec import AnalyticSpec
from parseus.interfaces.config import AnalyticConfiguration, get_analytic_config


@pytest.fixture(autouse=True)
def isolated_registry():
    """Keep these tests from leaking registry state into other tests."""
    snapshot = registry_module._ANALYTIC_SPECS.copy()
    registry_module._ANALYTIC_SPECS.clear()
    try:
        yield
    finally:
        registry_module._ANALYTIC_SPECS.clear()
        registry_module._ANALYTIC_SPECS.update(snapshot)


def _register_spec(analytic_name="my_analytic", **overrides):
    """Register a minimal spec for config-resolution tests."""
    defaults = {
        "key": analytic_name,
        "class_path": "tests.fake.FakeAnalytic",
        "run_idx": {"index_name": "run-from-spec"},
        "out_idx": {"index_name": f"alerts-{analytic_name}"},
    }
    defaults.update(overrides)

    spec = AnalyticSpec(**defaults)
    register_spec(spec, replace=True)
    return spec


def test_defaults_when_no_user_input():
    """Defaults come from the registered analytic spec when no user input exists."""
    _register_spec(
        "my_analytic",
        run_idx={"index_name": "run-from-spec"},
        out_idx={},
    )

    conf = get_analytic_config(user_input={}, analytic_name="my_analytic")

    assert isinstance(conf, AnalyticConfiguration)

    # Required/default runtime fields come from the registered spec.
    assert conf.name == "my_analytic"
    assert conf.run_idx == "run-from-spec"

    # Default out_idx when none is provided by spec or user input.
    assert conf.out_idx == "my_analytic-out"

    # Everything else should be default / zero / empty.
    assert conf.fit_idx == ""
    assert conf.fit_start is None
    assert conf.fit_end is None
    assert conf.run_start is None
    assert conf.run_end is None
    assert conf.user_save == ""
    assert conf.ip_filter == ""
    assert conf.schedule == 0
    assert conf.pull_duration == 0
    assert conf.suppression == "0s"
    assert conf.datastore_host_url == ""
    assert conf.tags == []


def test_user_input_overrides_spec_defaults_and_type_conversion():
    """User input overrides registered spec defaults."""
    _register_spec(
        "my_analytic",
        run_idx={"index_name": "run-spec"},
        fit_idx={
            "index_name": "fit-spec",
            "start": "2020-01-01",
            "end": "2020-01-02",
        },
        out_idx={"index_name": "out-spec"},
        schedule=3,
        pull_duration=6,
        suppression="24h",
        init_args={"alpha": 0.1},
    )

    user_input = {
        "run_idx": {"index_name": "run-ui"},
        "fit_idx": {"index_name": "fit-ui"},
        "out_idx": {"index_name": "out-ui"},
        "fit_start": "2021-12-31",
        "fit_end": "2022-01-02",
        "run_start": "2022-02-01",
        "run_end": "2022-02-02",
        "schedule": "7",
        "pull_duration": "12",
        "suppression": "0s",
        "user_save": "mysave",
        "ip_filter": "1.2.3.4/32",
        "ds_host_url": SimpleNamespace(url="http://user-host"),
    }

    conf = get_analytic_config(user_input=user_input, analytic_name="my_analytic")

    # User index selections override spec defaults.
    assert conf.run_idx == "run-ui"
    assert conf.fit_idx == "fit-ui"
    assert conf.out_idx == "out-ui"

    # User time windows override spec defaults.
    assert conf.fit_start == "2021-12-31"
    assert conf.fit_end == "2022-01-02"
    assert conf.run_start == "2022-02-01"
    assert conf.run_end == "2022-02-02"

    # Numeric strings are converted.
    assert isinstance(conf.schedule, int)
    assert conf.schedule == 7
    assert isinstance(conf.pull_duration, int)
    assert conf.pull_duration == 12

    # Suppression user input overrides spec default.
    assert conf.suppression == "0s"

    # init_args fall back to the spec when not overridden.
    assert conf.init_args == {"alpha": 0.1}

    # Misc user fields.
    assert conf.user_save == "mysave"
    assert conf.ip_filter == "1.2.3.4/32"
    assert conf.datastore_host_url == "http://user-host"


def test_user_input_init_args_override_spec_init_args():
    _register_spec(
        "my_analytic",
        init_args={"alpha": 0.1, "beta": 0.2},
    )

    conf = get_analytic_config(
        user_input={"init_args": {"alpha": 0.9}},
        analytic_name="my_analytic",
    )

    assert conf.init_args == {"alpha": 0.9}


@pytest.mark.parametrize(
    "spec_out_idx_tags, expected_set",
    [
        ("solo", {"solo", "base"}),
        (["a", "b", "a"], {"a", "b", "base"}),
    ],
)
def test_tags_are_collected_and_deduped_from_spec(spec_out_idx_tags, expected_set):
    _register_spec(
        "my_analytic",
        tags=["base"],
        out_idx={
            "index_name": "outfile",
            "tags": spec_out_idx_tags,
        },
    )

    conf = get_analytic_config(user_input={}, analytic_name="my_analytic")

    assert set(conf.tags) == expected_set
    assert len(conf.tags) == len(expected_set)


def test_user_out_idx_tags_are_collected_and_deduped():
    _register_spec(
        "my_analytic",
        tags=["base"],
        out_idx={
            "index_name": "outfile",
            "tags": ["spec", "shared"],
        },
    )

    conf = get_analytic_config(
        user_input={
            "out_idx": {
                "index_name": "out-ui",
                "tags": ["ui", "shared"],
            }
        },
        analytic_name="my_analytic",
    )

    assert conf.out_idx == "out-ui"
    assert set(conf.tags) == {"base", "spec", "shared", "ui"}


def test_registered_spec_is_authoritative():
    """A registered analytic resolves directly from its descriptor."""
    _register_spec(
        "RegistryOnlyAnalytic",
        out_idx={"index_name": "alerts-registry-only"},
    )

    conf = get_analytic_config(
        user_input={},
        analytic_name="RegistryOnlyAnalytic",
    )

    assert conf.out_idx == "alerts-registry-only"


def test_unknown_analytic_raises_keyerror():
    """An analytic not present in the registry raises clearly."""
    with pytest.raises(KeyError, match="not registered"):
        get_analytic_config(user_input={}, analytic_name="DoesNotExist")


def test_suppression_defaults_to_zero_seconds():
    _register_spec("my_analytic")

    conf = get_analytic_config(user_input={}, analytic_name="my_analytic")

    assert conf.suppression == "0s"


def test_suppression_from_registered_spec():
    _register_spec("my_analytic", suppression="24h")

    conf = get_analytic_config(user_input={}, analytic_name="my_analytic")

    assert conf.suppression == "24h"


def test_suppression_user_input_overrides_spec():
    _register_spec("my_analytic", suppression="24h")

    conf = get_analytic_config(
        user_input={"suppression": "0s"},
        analytic_name="my_analytic",
    )

    assert conf.suppression == "0s"


def test_user_ds_host_url_can_be_plain_string():
    _register_spec("my_analytic")

    conf = get_analytic_config(
        user_input={"ds_host_url": "http://user-host"},
        analytic_name="my_analytic",
    )

    assert conf.datastore_host_url == "http://user-host"


def test_none_user_input_is_supported():
    _register_spec("my_analytic")

    conf = get_analytic_config(user_input=None, analytic_name="my_analytic")

    assert conf.name == "my_analytic"
    assert conf.run_idx == "run-from-spec"


def test_spec_with_unset_fields_falls_back_to_defaults(monkeypatch):
    from parseus.interfaces import config

    spec = SimpleNamespace(
        fit_idx=None,
        run_idx=None,
        out_idx=None,
        init_args=None,
        schedule=None,
        pull_duration=None,
        tags=None,
    )
    monkeypatch.setattr(config, "get_analytic_spec", lambda name: spec)
    conf = get_analytic_config(user_input={"suppression": ""}, analytic_name="bare")
    assert conf.out_idx == "bare-out"
    assert conf.fit_idx == conf.run_idx == ""
    assert conf.init_args == {}
    assert conf.schedule == 0 and conf.pull_duration == 0
    assert conf.suppression == "0s"
    assert conf.tags == []
