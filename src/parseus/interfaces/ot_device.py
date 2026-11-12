"""
Representation of the data that PEAT places in an Elasticsearch datastore.
"""

from collections.abc import Mapping


def _dig(data, *keys, strip=True):
    """Return ``data[k1][k2]...`` (stripped if a string), or None if absent."""
    for key in keys:
        if not isinstance(data, Mapping) or key not in data:
            return None
        data = data[key]
    return data.strip() if strip and isinstance(data, str) else data


class OTDevice:
    """
    Takes in a dictionary glob from returned Elastic Search Data Query.
    Attempts to create a unique object from Elastic Search Data.
    """

    def __init__(self, data):
        self.host_data = data.get("host")
        host = self.host_data
        self.device_name = _dig(host, "hostname")
        self.full_description = _dig(host, "description", "full")
        self.host_ip = _dig(host, "ip")
        self.vendor = _dig(host, "description", "vendor", "id")
        self.brand = _dig(host, "description", "brand")
        self.model = _dig(host, "description", "model")
        self.interface = None
        self.ssh_keys = _dig(host, "ssh_keys", strip=False)
        self.device_identifier = f"{self.full_description or ''} {self.host_ip or ''}"
        self.device_timestamp = data["@timestamp"]

    def __str__(self) -> str:
        ret = f"HostName: {self.device_name} \n"
        ret += f"Vendor: {self.vendor} \n"
        ret += f"Brand: {self.brand} \n"
        ret += f"Model: {self.model} \n"
        ret += f"Full Description:: {self.full_description} \n"
        ret += f"IP: {self.host_ip} \n"
        ret += f"Timestamp: {self.device_timestamp}"
        return ret


def find_device(otdevice, device_set):
    for device in device_set:
        if otdevice.device_identifier == device.device_identifier:
            return device
    return None
