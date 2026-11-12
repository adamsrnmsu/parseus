"""Characterization tests for register annotation."""

import numpy as np
import pandas as pd
import pytest

from parseus.preprocessors.reg_annotations import (
    assign_register_type,
    classify_registers,
    columns_by_annotation,
    create_annotated_reg,
    create_nuniques,
)


@pytest.mark.parametrize(
    "value,name,expected",
    [
        (0, None, "Unchanging"),
        (1, "point_x_binary", "Unchanging"),
        (2, None, "Binary"),
        (3, None, "Digital"),
        (10, None, "Digital"),
        (11, None, "Analog"),
        (11, "a->b:point_Binary_1", "Binary"),
        (11, "a->b:point_Analog_Input_Binary_1", "Analog"),
        (11, "a->b:point_Analog_1", "Analog"),
        (11, "a->b:Binary_1", "Analog"),
    ],
)
def test_assign_register_type(value, name, expected):
    assert assign_register_type(value, name=name) == expected


@pytest.fixture
def reg_state():
    return pd.DataFrame(
        {
            "10.0.1.5->10.0.0.9:modbus_HR_1": [1.0, 1.0, 1.0],
            "10.0.1.6->10.0.0.9:modbus_HR_1": [0.0, 1.0, 0.0],
            "10.0.1.5->10.0.0.9:modbus_HR_2": [1.0, 2.0, np.nan],
            "10.0.1.5->10.0.0.9:modbus_HR_3": [1.0, 2.0, 3.0],
        }
    )


def test_nuniques_ignores_nan(reg_state):
    assert create_nuniques(reg_state).tolist() == [1, 2, 2, 3]


def test_classify_registers(reg_state):
    assert classify_registers(reg_state).tolist() == [
        "Unchanging",
        "Binary",
        "Binary",
        "Digital",
    ]


def test_annotated_reg_without_types_file(reg_state):
    ann = create_annotated_reg(reg_state)
    assert list(ann.index) == list(reg_state.columns)
    assert ann["Num_Unique"].tolist() == [1, 2, 2, 3]
    assert set(ann["Data_Type"]) == {"unknown"}


def test_annotated_reg_with_types_file(reg_state, tmp_path):
    types = tmp_path / "types.csv"
    types.write_text(
        # exact per-destination entry, then an all-destinations entry keyed by last
        # octet of the sender
        "10.0.1.6->10.0.0.9:modbus_HR_1,exact\n"
        "10.0.1.5:modbus_HR_2,wide\n"
    )
    ann = create_annotated_reg(reg_state, types_file=str(types))
    assert ann["Data_Type"].to_dict() == {
        "10.0.1.5->10.0.0.9:modbus_HR_1": "unknown",
        "10.0.1.6->10.0.0.9:modbus_HR_1": "exact",
        "10.0.1.5->10.0.0.9:modbus_HR_2": "wide",
        "10.0.1.5->10.0.0.9:modbus_HR_3": "unknown",
    }


def test_annotated_reg_types_file_sender_without_arrow(tmp_path):
    state = pd.DataFrame({"5:reg_1": [1.0, 2.0]})
    types = tmp_path / "types.csv"
    types.write_text("1.2.3.5:reg_1,thing\n")
    # sender is matched by its trailing octet
    ann = create_annotated_reg(state, types_file=str(types))
    assert ann["Data_Type"].tolist() == ["thing"]


def test_columns_by_annotation():
    ann = pd.DataFrame(
        {"Reg_Type": ["Binary", "Analog", "Binary"]}, index=["a", "b", "c"]
    )
    result = columns_by_annotation(ann)
    assert result["Binary"] == ["a", "c"]
    assert result["Analog"] == ["b"]
    assert result["Digital"] == [] and result["Unchanging"] == []
