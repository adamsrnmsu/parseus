"""Characterization tests for registers.utility and registers.views.state_data."""

import json

import pandas as pd
import pytest
from django.test import RequestFactory

from parseus.webapp.registers import utility, views


def frame():
    idx = pd.to_datetime(
        [
            "2024-01-01 00:00:00",
            "2024-01-01 00:00:10",
            "2024-01-01 00:00:20",
            "2024-01-01 00:00:30",
        ]
    )
    return pd.DataFrame(
        {
            "10.0.0.1->10.0.0.2:point_Counter_0": [1.0, 2.0, 3.0, 4.0],
            "10.0.0.1->10.0.0.2:point_Counter_1": [5.0, 5.0, 5.0, 5.0],
            "10.0.0.3->10.0.0.2:point_Analog_7": [0.5, 0.1, 0.9, 0.2],
        },
        index=idx,
    )


# ---- calculate_resample -------------------------------------------------------


def ts(**delta):
    base = pd.Timestamp("2024-01-01")
    return base + pd.Timedelta(**delta), base


@pytest.mark.parametrize(
    "delta, expected",
    [
        ({"seconds": 30}, "3s"),
        ({"seconds": 60}, "6s"),  # exactly 60s stays in seconds
        ({"minutes": 5}, "1min"),
        ({"hours": 1}, "6min"),  # exactly 60min stays in minutes
        ({"hours": 2}, "1h"),
        ({"hours": 24}, "3h"),  # exactly 24h stays in hours
        ({"days": 2}, "1d"),
        ({"days": 365}, "37d"),  # exactly 365d stays in days
        ({"days": 400}, "1YE"),
        ({"days": 3650}, "1YE"),
    ],
)
def test_calculate_resample(delta, expected):
    assert utility.calculate_resample(*ts(**delta)) == expected


# ---- get_conversations_from_reg_state ----------------------------------------


def test_conversations_grouped_by_src_dst_with_stats_and_flags():
    result = utility.get_conversations_from_reg_state(frame())
    assert set(result) == {"10.0.0.1->10.0.0.2", "10.0.0.3->10.0.0.2"}
    pair = result["10.0.0.1->10.0.0.2"]
    assert set(pair) == {"point_Counter_0", "point_Counter_1"}
    assert pair["point_Counter_0"]["constant"] is False
    assert pair["point_Counter_1"]["constant"] is True
    # The numeric prefixes depend on set ordering; the register suffix is stable.
    names = [
        pair["point_Counter_0"]["short_name"],
        pair["point_Counter_1"]["short_name"],
        result["10.0.0.3->10.0.0.2"]["point_Analog_7"]["short_name"],
    ]
    assert [n.rsplit(":", 1)[1] for n in names] == ["0", "1", "7"]
    assert len(set(names)) == 3
    assert names[0].rsplit(":", 1)[0] == names[1].rsplit(":", 1)[0]
    assert {"min", "max", "mean", "std"} <= set(pair["point_Counter_0"])
    assert pair["point_Counter_0"]["min"] == 1.0
    assert pair["point_Counter_0"]["max"] == 4.0
    assert "points" not in pair["point_Counter_0"]


def test_conversations_include_points_for_graphing():
    result = utility.get_conversations_from_reg_state(frame(), include_points=True)
    points = result["10.0.0.1->10.0.0.2"]["point_Counter_0"]["points"]
    assert points[0] == (pd.Timestamp("2024-01-01 00:00:00"), 1.0)
    assert len(points) == 4


# ---- summary / counts ---------------------------------------------------------


def test_summary_and_counts():
    summary = utility.get_summary_from_reg_state(frame())
    assert summary == {
        "timeline": {
            "length": "0 days 00:00:30",
            "start_end": "January 01 2024 00:00:00.000 - January 01 2024 00:00:30.000",
        },
        "conversations": 3,
        "unique_timestamps": 4,
    }
    assert utility.get_counts_from_reg_state(frame()) == {
        "total": 3,
        "list": "1 constant, 2 variable",
    }


# ---- state_data ---------------------------------------------------------------


def state_request(graph=None, registers=("a", "b"), session=True):
    request = RequestFactory().get(
        "/state_data", {"graph": graph, "registers[]": list(registers)} if graph else {}
    )
    request.session = (
        {"reg_state": pd.DataFrame({"a": [1, 2], "b": [3, 4], "c": [5, 6]}).to_json()}
        if session
        else {}
    )
    return request


@pytest.fixture
def seen(monkeypatch):
    calls = {}

    def record(name):
        def fn(*args):
            calls[name] = [list(a.columns) if hasattr(a, "columns") else a for a in args]
            return f"{name}-result"

        return fn

    monkeypatch.setattr(utility, "get_clustered_states_dict", record("states"))
    monkeypatch.setattr(utility, "get_reg_updates_heatmap_dict", record("heatmap"))
    monkeypatch.setattr(utility, "get_node_edge_dict", record("node_edge"))
    return calls


def test_state_data_over_time_uses_filtered_registers(seen):
    response = views.state_data(state_request("Register State Over Time"))
    assert json.loads(response.content) == {"states": "states-result"}
    assert seen["states"] == [["a", "b"]]


def test_state_data_heatmap_gets_filtered_and_original_state(seen):
    response = views.state_data(state_request("Register Updates Heatmap"))
    assert json.loads(response.content) == {"heatmap": "heatmap-result"}
    assert seen["heatmap"] == [["a", "b"], ["a", "b", "c"]]


def test_state_data_transition_graph_uses_state_id_column(seen):
    response = views.state_data(state_request("State Transition Graph"))
    assert json.loads(response.content) == {"node_edge": "node_edge-result"}
    assert seen["node_edge"] == [["a", "b"], "state_id"]


def test_state_data_unknown_graph_is_empty(seen):
    assert json.loads(views.state_data(state_request("Nope")).content) == {}
    assert seen == {}


def test_state_data_missing_session_or_register_is_400(seen):
    assert views.state_data(state_request("Nope", session=False)).status_code == 400
    assert views.state_data(state_request("Nope", registers=("zzz",))).status_code == 400
