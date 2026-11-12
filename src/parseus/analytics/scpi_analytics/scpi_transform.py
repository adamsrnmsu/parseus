import re


def scpi_transform(documents, **kwargs):
    raw_data_fields = []
    for doc in documents:
        raw_data_fields.append(doc["data"])
    parsed_data = parse_data(raw_data_fields)
    value_ranges = get_value_ranges(parsed_data)
    return value_ranges


def parse_data(data_list: list) -> dict:
    """
    Create a dictionary with all values of each SCPI command seen

    Args:
        data_list: A list of un-parsed SCPI commands

    Return:
        A dict with all commands seen. If they have subcommands/vals those are in a subdictionary
    """
    parsed_data = {}
    for entry in data_list:
        initial_split = entry.split(" ")  # split on the space separator
        command = initial_split[0]
        if len(initial_split) > 1:
            value = initial_split[1]
        else:
            value = ""
        channel_match = re.search(
            r"@\d+", value
        )  # look for commands that have a specified channel value (ie "(@1)"" etc)
        if channel_match:
            secondary_split = value.split(",")
            sub_command_or_value = secondary_split[0]
            extracted_channel_value = int(
                channel_match.group(0)[1:]
            )  # Weird, but this just extracts the channel number from the regex search
            subcommands = {
                "sub_command_or_value": sub_command_or_value,
                "channel": extracted_channel_value,
            }
            if command not in parsed_data:
                parsed_data[command] = []
            parsed_data[command].append(subcommands)
        else:
            if command not in parsed_data:
                parsed_data[command] = []
            parsed_data[command].append(value)
    return parsed_data


def get_value_ranges(parsed_scpi_dict: dict):
    command_ranges = {}
    for scpi_command in parsed_scpi_dict.keys():
        if type(parsed_scpi_dict[scpi_command][0]) is dict:
            parsed_values_dict = get_range_from_dict(parsed_scpi_dict[scpi_command])
            command_ranges[scpi_command] = parsed_values_dict
        elif parsed_scpi_dict[scpi_command][0] == "":
            # case where command has no values associated
            command_ranges[scpi_command] = ""
        elif type(parsed_scpi_dict[scpi_command][0]) is str:
            # case where command has a range of values
            # first check for corner case where the list contains both dicts and string values
            unique_types = set()
            for item in parsed_scpi_dict[scpi_command]:
                unique_types.add(type(item))
            if len(unique_types) > 1:
                command_ranges[scpi_command] = handle_mixed_types(
                    parsed_scpi_dict[scpi_command]
                )
            else:
                try:
                    float_values = [float(s) for s in parsed_scpi_dict[scpi_command]]
                    data_range = {
                        "min": min(float_values),
                        "max": max(float_values),
                    }
                    command_ranges[scpi_command] = data_range
                except ValueError:
                    # case where command contains a list of string literals/labels
                    command_ranges[scpi_command] = set(parsed_scpi_dict[scpi_command])
    return command_ranges


def get_range_from_dict(values_dict: dict):
    channel_set = set()
    list_of_values = []
    for subcommand in values_dict:
        if type(subcommand) is dict:
            channel_set.add(subcommand["channel"])
            try:
                list_of_values.append(float(subcommand["sub_command_or_value"]))
            except ValueError:
                # sometimes values are just ON/OFF
                list_of_values.append(subcommand["sub_command_or_value"])
        else:
            list_of_values.append(subcommand)

    if type(list_of_values[0]) is float:
        return_dict = {
            "channels": channel_set,
            "min": min(list_of_values),
            "max": max(list_of_values),
        }
        return return_dict
    else:
        # list_of_values is just strings of ON/OFF
        return_dict = {"channels": channel_set, "values": list_of_values}
        return return_dict


def handle_mixed_types(values_list):
    """
    Handle a list from a command with both values (string ON/OFF) and subdict
    {sub_command_or_value, channel}
    """
    on_off_set = set()
    channels = set()
    for item in values_list:
        if type(item) is str:
            on_off_set.add(item)
        elif type(item) is dict:
            on_off_set.add(item["sub_command_or_value"])
            channels.add(item["channel"])
    return {"values": on_off_set, "channels": channels}
