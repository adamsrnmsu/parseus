#!/usr/bin/env python3
"""Build a small Modbus/TCP + DNP3 pcap with known register/point values.

    make_test_pcap.py OUT.pcap          write the pcap
    make_test_pcap.py --check LOGDIR    compare Zeek's modbus_detailed.log and
                                        dnp3_points.log in LOGDIR with the values below

Standard library only, so it runs anywhere. The pcap is the ground truth for
checking the Zeek script: every value is chosen on purpose (negative, 16-bit max,
float, prefixed index, packed bits).
"""
import json
import struct
import sys
from pathlib import Path

CLIENT, SERVER = "10.1.1.10", "10.1.1.20"

# (func, address, request_values|None, response_values|None) as Zeek should log them.
EXPECTED_MODBUS = [
    ("READ_HOLDING_REGISTERS", 100, None, [0, 1, 65535, 12345]),
    ("READ_INPUT_REGISTERS", 7, None, [500, 65000]),
    ("WRITE_SINGLE_REGISTER", 9, [4242], None),
    ("WRITE_MULTIPLE_REGISTERS", 20, [1, 2, 3], None),
    ("READ_COILS", 0, None, [1, 0, 1, 0, 0, 0, 0, 0, 0, 1]),
    ("WRITE_SINGLE_COIL", 3, [1], None),
    ("READ_DISCRETE_INPUTS", 4, None, [1, 0, 1]),
]
# (group, variation, index, value, function_code)
EXPECTED_DNP3 = (
    [(30, 1, 10 + i, v, "RESPONSE") for i, v in enumerate([100, -5, 2000000000])]
    + [(30, 2, 20 + i, v, "RESPONSE") for i, v in enumerate([300, -300])]
    + [(30, 5, 30 + i, v, "RESPONSE") for i, v in enumerate([3.5, -0.15625])]
    + [(20, 1, i, v, "RESPONSE") for i, v in enumerate([1000, 4294967295])]
    + [(1, 1, i, 1 if i in (0, 2, 9) else 0, "RESPONSE") for i in range(10)]
    + [(1, 2, 7, 1, "RESPONSE"), (1, 2, 42, 0, "RESPONSE")]
    + [(30, 1, 300, 77, "RESPONSE"), (30, 1, 1000, -1, "RESPONSE")]
    # 4-byte start/stop range (q=0x02) keeps its real start index.
    + [(30, 1, 70000, 33, "RESPONSE"), (30, 1, 70001, 44, "RESPONSE")]
    # g30v5 inf at index 40 is dropped (not a usable value); index 41 survives.
    + [(30, 5, 41, 1.5, "RESPONSE")]
    # g2v1 (binary event) is deliberately absent: stock Zeek raises no event with
    # its flag byte, so logging it would invent values.
)


def _csum(data):
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def _ip(addr):
    return bytes(int(p) for p in addr.split("."))


class Flow:
    """One TCP connection, written as ethernet frames."""

    def __init__(self, out, sport, dport):
        self.out, self.sport, self.dport = out, sport, dport
        self.seq = {True: 1000, False: 5000}
        self.t = 1_700_000_000.0 + sport
        self._send(True, b"", 0x02)
        self.seq[True] += 1
        self._send(False, b"", 0x12)
        self.seq[False] += 1
        self._send(True, b"", 0x10)

    def _send(self, from_client, payload, flags=0x18):
        src, dst = (CLIENT, SERVER) if from_client else (SERVER, CLIENT)
        sp, dp = (self.sport, self.dport) if from_client else (self.dport, self.sport)
        tcp = struct.pack("!HHIIBBHHH", sp, dp, self.seq[from_client],
                          self.seq[not from_client], 5 << 4, flags, 65535, 0, 0) + payload
        pseudo = _ip(src) + _ip(dst) + struct.pack("!BBH", 0, 6, len(tcp))
        tcp = tcp[:16] + struct.pack("!H", _csum(pseudo + tcp)) + tcp[18:]
        ip = struct.pack("!BBHHHBBH", 0x45, 0, 20 + len(tcp), 1, 0, 64, 6, 0) + _ip(src) + _ip(dst)
        ip = ip[:10] + struct.pack("!H", _csum(ip)) + ip[12:]
        frame = b"\x02\0\0\0\0\x02" + b"\x02\0\0\0\0\x01" + b"\x08\x00" + ip + tcp
        self.t += 0.01
        sec = int(self.t)
        self.out.append(struct.pack("<IIII", sec, int((self.t - sec) * 1e6), len(frame), len(frame)) + frame)

    def push(self, from_client, payload):
        self._send(from_client, payload)
        self.seq[from_client] += len(payload)


def modbus(flow, tid, unit, req, resp):
    for from_client, pdu in ((True, req), (False, resp)):
        flow.push(from_client, struct.pack("!HHHB", tid, 0, len(pdu) + 1, unit) + pdu)


def dnp3_crc(data):
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA6BC if crc & 1 else crc >> 1
    return struct.pack("<H", ~crc & 0xFFFF)


def dnp3_frame(src, dst, app, from_master):
    data = b"\xC0" + app  # transport: FIR | FIN
    head = struct.pack("<BBBBHH", 5, 0x64, 5 + len(data), 0xC4 if from_master else 0x44, dst, src)
    out = head + dnp3_crc(head)
    for i in range(0, len(data), 16):
        out += data[i:i + 16] + dnp3_crc(data[i:i + 16])
    return out


def dnp3_response(flow, objects):
    app = struct.pack("<BBH", 0xC0, 0x81, 0) + objects
    flow.push(False, dnp3_frame(1024, 1, app, False))


def build():
    pkts = []
    mb = Flow(pkts, 40000, 502)
    modbus(mb, 1, 1, struct.pack("!BHH", 3, 100, 4), b"\x03\x08" + struct.pack("!4H", 0, 1, 65535, 12345))
    modbus(mb, 2, 1, struct.pack("!BHH", 4, 7, 2), b"\x04\x04" + struct.pack("!2H", 500, 65000))
    modbus(mb, 3, 1, struct.pack("!BHH", 6, 9, 4242), struct.pack("!BHH", 6, 9, 4242))
    modbus(mb, 4, 1, struct.pack("!BHHB3H", 16, 20, 3, 6, 1, 2, 3), struct.pack("!BHH", 16, 20, 3))
    modbus(mb, 5, 1, struct.pack("!BHH", 1, 0, 10), b"\x01\x02\x05\x02")
    modbus(mb, 6, 1, struct.pack("!BHH", 5, 3, 0xFF00), struct.pack("!BHH", 5, 3, 0xFF00))
    modbus(mb, 7, 1, struct.pack("!BHH", 2, 4, 3), b"\x02\x01\x05")

    dn = Flow(pkts, 41000, 20000)
    dn.push(True, dnp3_frame(1, 1024, b"\xC0\x01\x1e\x00\x06", True))  # READ all analog inputs
    dnp3_response(dn, struct.pack("<BBBBB", 30, 1, 0, 10, 12)
                  + b"".join(struct.pack("<Bi", 1, v) for v in (100, -5, 2000000000)))
    dnp3_response(dn, struct.pack("<BBBBB", 30, 2, 0, 20, 21)
                  + b"".join(struct.pack("<Bh", 1, v) for v in (300, -300)))
    dnp3_response(dn, struct.pack("<BBBBB", 30, 5, 0, 30, 31)
                  + b"".join(struct.pack("<Bf", 1, v) for v in (3.5, -0.15625)))
    dnp3_response(dn, struct.pack("<BBBBB", 20, 1, 0, 0, 1)
                  + b"".join(struct.pack("<BI", 1, v) for v in (1000, 4294967295)))
    dnp3_response(dn, struct.pack("<BBBBB", 1, 1, 0, 0, 9) + bytes([0b00000101, 0b00000010]))
    dnp3_response(dn, struct.pack("<BBBB", 1, 2, 0x17, 2) + bytes([7, 0x81, 42, 0x01]))
    dnp3_response(dn, struct.pack("<BBBH", 30, 1, 0x28, 2)
                  + struct.pack("<HBi", 300, 1, 77) + struct.pack("<HBi", 1000, 1, -1))
    dnp3_response(dn, struct.pack("<BBBII", 30, 1, 0x02, 70000, 70001)
                  + b"".join(struct.pack("<Bi", 1, v) for v in (33, 44)))
    dnp3_response(dn, struct.pack("<BBBBB", 30, 5, 0, 40, 41)
                  + struct.pack("<BI", 1, 0x7F800000) + struct.pack("<Bf", 1, 1.5))
    dnp3_response(dn, struct.pack("<BBBB", 2, 1, 0x17, 3) + bytes([5, 0x81, 6, 0x01, 7, 0x00]))
    # g40v1 analog output status: stock Zeek raises no event for it, so it is not expected.
    dnp3_response(dn, struct.pack("<BBBBB", 40, 1, 0, 5, 5) + struct.pack("<Bi", 1, 5000))
    return pkts


def write_pcap(path, pkts):
    with open(path, "wb") as fh:
        fh.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        fh.write(b"".join(pkts))


def _rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.startswith("{")]


def check(logdir):
    got_mb = [(r["func"], r["address"], r.get("request_values"), r.get("response_values"))
              for r in _rows(Path(logdir) / "modbus_detailed.log")
              if r.get("request_values") or r.get("response_values")]
    got_dn = [(r["group"], r["variation"], r["index"], r["value"], r["function_code"])
              for r in _rows(Path(logdir) / "dnp3_points.log")]
    bad = [f"modbus: {got_mb}\n   != {EXPECTED_MODBUS}"] if got_mb != EXPECTED_MODBUS else []
    if got_dn != EXPECTED_DNP3:
        bad.append(f"dnp3 missing={[x for x in EXPECTED_DNP3 if x not in got_dn]} "
                   f"extra={[x for x in got_dn if x not in EXPECTED_DNP3]}")
    print("\n".join(bad) or f"OK: {len(got_mb)} modbus rows, {len(got_dn)} dnp3 points match")
    return 1 if bad else 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        sys.exit(check(sys.argv[2]))
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    write_pcap(sys.argv[1], build())
