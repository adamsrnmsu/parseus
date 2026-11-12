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


_REG_TYPE_BY_FUNC = {
    "READ_HOLDING_REGISTERS": "HR",
    "WRITE_MULTIPLE_REGISTERS": "HR",
    "WRITE_SINGLE_REGISTER": "HR",
    "READ_COILS": "C",
    "WRITE_SINGLE_COIL": "C",
    "WRITE_MULTIPLE_COILS": "C",
    "READ_INPUT_REGISTERS": "IR",
    "READ_DISCRETE_INPUTS": "DI",
}


def _modbus_fields(packet):
    """Return (detailed, func_code, values) for a usable packet, else None."""
    zeek = packet.get("zeek") or {}
    detailed = zeek.get("modbus_detailed") or {}
    func_code = (zeek.get("modbus") or {}).get("func")
    if detailed.get("address") is None or func_code is None:
        return None
    values = detailed.get("response_values", detailed.get("request_values"))
    if not values:
        return None
    return detailed, func_code, values


def modbus_to_regop(packet):
    fields = _modbus_fields(packet)
    if fields is None:
        return []
    detailed, func_code, modbus_values = fields
    timestamp = make_zulu_time(datetime_parser(packet["@timestamp"]))
    packet_type = "modbus"
    source = packet["source"]["ip"]
    destination = packet["destination"]["ip"]
    regops = [(timestamp, packet_type, source, destination, "MODBUS_PRESENT", 1)]
    reg_type = _REG_TYPE_BY_FUNC.get(func_code)
    if reg_type is None:
        # unmapped function code: we know the device spoke, but not which
        # register space the values belong to
        return regops
    for i, value in enumerate(modbus_values, start=int(detailed["address"])):
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


def packet_to_regops(packet, **kwargs):
    """
    Takes a packet formatted like it came out of Zeek output (Modbus only)
    and converts it into a list of regop(s)

    :param packet: The packet dictionary
    :return: A list of regop(s) contained in this packet
    """
    if "zeek" in packet:
        result = modbus_to_regop(packet)
    else:
        return []
    return result
