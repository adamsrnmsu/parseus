"""
Contains operations for analyzing a list of register operation documents from
Elasticsearch, including functions to separate the packets by sender
"""

from parseus.util.datetime_utils import datetime_parser, make_zulu_time

# These constants are indices into regops tuples

TIMESTAMP = 0
PACKET_TYPE = 1
SOURCE = 2
DESTINATION = 3
ADDRESS = 4
VALUE = 5


def modbus_to_regop(packet):
    if (
        packet.get("zeek") is None
        or packet["zeek"].get("modbus_detailed") is None
        or packet["zeek"]["modbus_detailed"].get("address") is None
        or packet["zeek"].get("modbus") is None
        or packet["zeek"]["modbus"].get("func") is None
    ):
        return []
    modbus_values = packet["zeek"]["modbus_detailed"].get(
        "response_values", packet["zeek"]["modbus_detailed"].get("request_values", None)
    )
    if not modbus_values:
        return []
    timestamp = make_zulu_time(datetime_parser(packet["@timestamp"]))
    packet_type = "modbus"
    source = packet["source"]["ip"]
    destination = packet["destination"]["ip"]
    address = "MODBUS_PRESENT"
    value = 1
    start_address = int(packet["zeek"]["modbus_detailed"].get("address"))
    regops = [(timestamp, packet_type, source, destination, address, value)]
    func_code = packet["zeek"]["modbus"]["func"]
    if func_code in (
        "READ_HOLDING_REGISTERS",
        "WRITE_MULTIPLE_REGISTERS",
        "WRITE_SINGLE_REGISTER",
    ):
        reg_type = "HR"
    elif func_code in ("READ_COILS", "WRITE_SINGLE_COIL", "WRITE_MULTIPLE_COILS"):
        reg_type = "C"
    elif func_code == "READ_INPUT_REGISTERS":
        reg_type = "IR"
    elif func_code == "READ_DISCRETE_INPUTS":
        reg_type = "DI"
    for i, value in enumerate(modbus_values, start=start_address):
        # Attempt to add the register operation to our return value
        try:
            regops.append(
                (
                    timestamp,
                    packet_type,
                    source,
                    destination,
                    f"modbus_{reg_type}_{i}",
                    float(value),
                )
            )
        except ValueError:
            continue
    return regops


def dnp3_to_regop(packet):
    """Convert a Zeek ``dnp3_points`` document into a single regop (or none)."""
    point = (packet.get("zeek") or {}).get("dnp3_points")
    if not point or point.get("value") is None or point.get("index") is None:
        return []
    return [
        (
            make_zulu_time(datetime_parser(packet["@timestamp"])),
            "dnp3",
            packet["source"]["ip"],
            packet["destination"]["ip"],
            f"dnp3_{point['object_type']}_{point['index']}",
            float(point["value"]),
        )
    ]


def packet_to_regops(packet, **kwargs):
    """
    Takes a packet formatted like it came out of Zeek output (Modbus only)
    and converts it into a list of regop(s)

    :param packet: The packet dictionary
    :return: A list of regop(s) contained in this packet
    """
    if "zeek" in packet:
        if "dnp3_points" in packet["zeek"]:
            return dnp3_to_regop(packet)
        result = modbus_to_regop(packet)
    else:
        return []
    return result
