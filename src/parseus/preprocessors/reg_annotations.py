"""
Creates a dataframe that contains annotated information about registers and
their value types and ranges
"""

import numpy as np
import pandas as pd


def assign_register_type(value, name=None):
    """
    Classifies register [signal] into a given type based on number of unique
    values in the set
    """
    if value <= 1:
        return "Unchanging"

    if name is not None and "point_" in name and "_Input_" not in name:
        if "binary" in name.lower():
            return "Binary"

    if 1 < value <= 2:
        return "Binary"
    if 2 < value <= 10:
        return "Digital"
    return "Analog"


def classify_registers(reg_state):
    """
    Takes in a pandas dataframe and for each src, dst, reg, classifies the
    reg [signal] as Unchanging, Binary, Digital, Analog
    Returns: pd.Series
    """
    cols = reg_state.columns
    register_types = pd.Series(index=reg_state.columns, dtype="str")
    for col in cols:
        temp_series = reg_state[col]
        classification = assign_register_type(
            create_nuniques(temp_series.to_frame()).iloc[0], name=col
        )
        register_types[col] = classification
    return register_types


def create_nuniques(reg_state):
    """
    Takes in a pandas dataframe and for each src, dst, reg, finds the number of
    unique values that triplet takes on
    Returns: pd.Series
    """
    cols = reg_state.columns
    nuniques = pd.Series(index=reg_state.columns, dtype="float64")
    for col in cols:
        temp_series = reg_state[col].values.astype(np.float32)
        number_unique = len(np.unique(temp_series[~np.isnan(temp_series)]))
        nuniques[col] = number_unique
    return nuniques


def _last_octet(sender):
    return sender[sender.rfind(".") + 1 :] if "." in sender else sender


def _sender_and_register(index_value):
    """Split a register name into (last octet of sender, register)."""
    if "->" in index_value:
        sender = _last_octet(index_value[: index_value.index("->")])
    else:
        sender = index_value[: index_value.index(":")]
    return sender, index_value[index_value.find(":") + 1 :]


def _apply_types_file(annotated_reg, types_file):
    """Fill the Data_Type column from a CSV of register -> data type."""
    mapped_registers = {
        index_value: _sender_and_register(index_value)
        for index_value in annotated_reg.index.values
    }
    for register, data_type in pd.read_csv(types_file, header=None).values:
        if "->" in register:
            # for just one destination
            annotated_reg.loc[register, "Data_Type"] = data_type
            continue
        # for all destinations
        sender, reg_number = register.split(":")
        key = (_last_octet(sender), reg_number)
        for index_value, mapped in mapped_registers.items():
            if mapped == key:
                annotated_reg.loc[index_value, "Data_Type"] = data_type


def create_annotated_reg(reg_state, types_file=None):
    """
    Creates an annotated register frame to display categorical data about
    registers
    """
    annotated_reg = pd.DataFrame(index=reg_state.columns)
    annotated_reg["Reg_Type"] = classify_registers(reg_state)
    annotated_reg["Num_Unique"] = create_nuniques(reg_state)
    annotated_reg["Data_Type"] = pd.Series(index=annotated_reg.index, data="unknown")

    if types_file is not None:
        _apply_types_file(annotated_reg, types_file)

    return annotated_reg


def columns_by_annotation(reg_annotations):
    """
    Groups the registers by annotation

    :param reg_annotations: The annotations DataFrame
    :return: A dictionary of Reg_Type => list of registers with that type
    """
    annotations = {"Binary", "Digital", "Analog", "Unchanging"}
    return {
        annotation: reg_annotations.index.values[
            np.argwhere(reg_annotations["Reg_Type"].values == annotation).flatten()
        ].tolist()
        for annotation in annotations
    }
