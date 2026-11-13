#!/usr/bin/env python3
"""Build a small Modbus/TCP + DNP3 pcap with known register/point values.

    make_test_pcap.py OUT.pcap          write the pcap
    make_test_pcap.py --check LOGDIR    compare Zeek's modbus_detailed.log and
                                        dnp3_points.log in LOGDIR with the values below

Standard library only, so it runs anywhere. The pcap is the ground truth for
checking the Zeek script: every value is chosen on purpose (negative, 16-bit max,
float, prefixed index, packed bits). Rows for g40 and g2 come from the Spicy
analyzer (spicy/parseus_dnp3.spicy), everything else from stock Zeek; the last
DNP3 flow (port 41001) feeds the Spicy walker frames it must refuse.
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
    # g30v6 double precision (what ferrofire/bennu outstations send for analog inputs);
    # the +inf at index 53 is dropped like the g30v5 inf.
    + [(30, 6, 50 + i, v, "RESPONSE") for i, v in enumerate([13.25, -1.5, 1048576.125])]
    + [(30, 6, 54, 0.0, "RESPONSE")]
    # g32v6 double-precision analog event, indexed by a 1-byte prefix (q=0x17).
    + [(32, 6, 60, 7.75, "RESPONSE")]
    # ---- Spicy analyzer: g40 analog output status, g2 binary input events.
    # g2 value is the state bit (bit 7) of the flag byte, like g1v2/g10v2.
    + [(40, 1, 5, 5000, "RESPONSE"), (40, 1, 6, -5000, "RESPONSE")]       # q=0x00 range
    + [(40, 2, 300, 1234, "RESPONSE"), (40, 2, 301, -1234, "RESPONSE")]   # q=0x01 range
    + [(40, 3, 12, 2.5, "RESPONSE"), (40, 3, 13, -0.75, "RESPONSE")]      # q=0x17; inf at 14 skipped
    + [(40, 4, 500, 1048576.125, "RESPONSE"),                             # q=0x28; NaN at 501 skipped
       (40, 4, 502, -0.0001220703125, "RESPONSE")]
    + [(2, 1, 5, 1, "RESPONSE"), (2, 1, 6, 0, "RESPONSE"), (2, 1, 7, 0, "RESPONSE")]
    # One response: stock g30/g20/g1 objects, then g40 and g2, then stock g30v6 again;
    # the Spicy walker must size every stock object to reach g40/g2.
    + [(30, 1, 80, 11, "RESPONSE"), (30, 1, 81, -11, "RESPONSE"), (20, 2, 7, 321, "RESPONSE")]
    + [(1, 1, 20 + i, 1 if i in (0, 7) else 0, "RESPONSE") for i in range(8)]
    + [(40, 1, 90, 77, "RESPONSE"), (2, 1, 3, 1, "RESPONSE"), (2, 1, 4, 0, "RESPONSE"),
       (30, 6, 91, 2.25, "RESPONSE")]
    # Unsolicited response: g2v2 (flag + 6-byte time) and g2v3 (flag + 2-byte time).
    + [(2, 2, 3, 1, "UNSOLICITED_RESPONSE"), (2, 2, 8, 0, "UNSOLICITED_RESPONSE"),
       (2, 3, 300, 0, "UNSOLICITED_RESPONSE")]
    # 40 points in a fragment split over 4 link frames (transport seq 62, 63, 0, 1).
    + [(40, 1, 1000 + i, i * 10 - 100, "RESPONSE") for i in range(40)]
    + [(2, 2, 9, 1, "RESPONSE")]
    # g80v1 (IIN bits, packed: 16 points in 2 bytes) before g40: the walker must size it as bits.
    + [(40, 1, 120, 42, "RESPONSE")]
    # Robustness flow: bad-CRC frame dropped, unknown object ends the walk, size-prefixed
    # unknown object is skipped, a segment gap drops the fragment, truncation keeps only
    # the items that are fully present.
    + [(40, 1, 98, 2, "RESPONSE"), (40, 1, 96, 3, "RESPONSE"), (40, 1, 95, 4, "RESPONSE"),
       (40, 1, 93, 6, "RESPONSE"), (40, 1, 80, 7, "RESPONSE"), (40, 1, 81, 8, "RESPONSE")]
)
# (group, variation, index) -> quality flags of the Spicy rows.
EXPECTED_FLAGS = {
    (40, 1, 5): 1, (40, 1, 6): 3, (40, 2, 300): 1, (40, 3, 12): 1, (40, 4, 502): 0x21,
    (2, 1, 5): 0x81, (2, 1, 6): 0x01, (2, 1, 7): 0, (40, 1, 90): 1, (2, 1, 3): 0x81,
    (2, 2, 3): 0x81, (2, 2, 8): 0x01, (2, 3, 300): 0x01, (40, 1, 1000): 1, (2, 2, 9): 0x81,
}


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


def dnp3_frame(src, dst, app, from_master, transport=0xC0, bad_crc=False):
    data = bytes([transport]) + app  # transport: FIR | FIN | seq
    head = struct.pack("<BBBBHH", 5, 0x64, 5 + len(data), 0xC4 if from_master else 0x44, dst, src)
    out = head + dnp3_crc(head)
    for i in range(0, len(data), 16):
        out += data[i:i + 16] + dnp3_crc(data[i:i + 16])
    if bad_crc:
        out = out[:-1] + bytes([out[-1] ^ 0xFF])
    return out


def dnp3_response(flow, objects, fc=0x81, seg=None, seq0=0, skip=(), bad_crc=()):
    """One response fragment; seg splits it into link frames of seg app bytes each
    (transport FIR/FIN/seq starting at seq0). Frames in `skip` are never sent,
    frames in `bad_crc` get a corrupted CRC."""
    app = struct.pack("<BBH", 0xC0, fc, 0) + objects
    parts = [app] if seg is None else [app[i:i + seg] for i in range(0, len(app), seg)]
    for i, part in enumerate(parts):
        tr = (0x40 if i == 0 else 0) | (0x80 if i == len(parts) - 1 else 0) | ((seq0 + i) & 0x3F)
        if i not in skip:
            flow.push(False, dnp3_frame(1024, 1, part, False, tr, i in bad_crc))


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
    dnp3_response(dn, struct.pack("<BBBBB", 30, 6, 0, 50, 54)
                  + b"".join(struct.pack("<Bd", 1, v) for v in (13.25, -1.5, 1048576.125, float("inf"), 0.0)))
    dnp3_response(dn, struct.pack("<BBBB", 32, 6, 0x17, 1) + struct.pack("<BBd", 60, 1, 7.75))
    dnp3_response(dn, struct.pack("<BBBB", 2, 1, 0x17, 3) + bytes([5, 0x81, 6, 0x01, 7, 0x00]))
    # g40 analog output status and g2 binary input events (the Spicy analyzer).
    g40 = lambda v, q, rng, items: struct.pack("<BBB", 40, v, q) + rng + items   # noqa: E731
    dnp3_response(dn, g40(1, 0x00, bytes([5, 6]), struct.pack("<Bi", 1, 5000) + struct.pack("<Bi", 3, -5000)))
    dnp3_response(dn, g40(2, 0x01, struct.pack("<HH", 300, 301), struct.pack("<Bh", 1, 1234) + struct.pack("<Bh", 1, -1234)))
    dnp3_response(dn, g40(3, 0x17, bytes([3]), struct.pack("<BBf", 12, 1, 2.5) + struct.pack("<BBf", 13, 1, -0.75)
                          + struct.pack("<BBI", 14, 1, 0x7F800000)))
    dnp3_response(dn, g40(4, 0x28, struct.pack("<H", 3), struct.pack("<HBd", 500, 1, 1048576.125)
                          + struct.pack("<HBQ", 501, 1, 0x7FF8000000000000) + struct.pack("<HBd", 502, 0x21, -0.0001220703125)))
    dnp3_response(dn, struct.pack("<BBBBB", 30, 1, 0, 80, 81) + b"".join(struct.pack("<Bi", 1, v) for v in (11, -11))
                  + struct.pack("<BBBBB", 20, 2, 0, 7, 7) + struct.pack("<BH", 1, 321)
                  + struct.pack("<BBBBB", 1, 1, 0, 20, 27) + bytes([0b10000001])
                  + g40(1, 0x00, bytes([90, 90]), struct.pack("<Bi", 1, 77))
                  + struct.pack("<BBBB", 2, 1, 0x17, 2) + bytes([3, 0x81, 4, 0x01])
                  + struct.pack("<BBBBB", 30, 6, 0, 91, 91) + struct.pack("<Bd", 1, 2.25))
    dnp3_response(dn, struct.pack("<BBBB", 2, 2, 0x17, 2) + bytes([3, 0x81]) + bytes(6) + bytes([8, 0x01]) + bytes(6)
                  + struct.pack("<BBBH", 2, 3, 0x28, 1) + struct.pack("<HBH", 300, 0x01, 0), fc=0x82)
    dnp3_response(dn, g40(1, 0x01, struct.pack("<HH", 1000, 1039),
                          b"".join(struct.pack("<Bi", 1, i * 10 - 100) for i in range(40)))
                  + struct.pack("<BBBB", 2, 2, 0x17, 1) + bytes([9, 0x81]) + bytes(6), seg=60, seq0=62)
    dnp3_response(dn, struct.pack("<BBBBB", 80, 1, 0, 0, 15) + bytes([0x00, 0x80])
                  + g40(1, 0x00, bytes([120, 120]), struct.pack("<Bi", 1, 42)))

    # Frames the Spicy walker must refuse (port 41001, so a stock-analyzer upset on the
    # unknown objects cannot touch the flow above).
    rb = Flow(pkts, 41001, 20000)
    one = lambda idx, v: g40(1, 0x00, bytes([idx, idx]), struct.pack("<Bi", 1, v))   # noqa: E731
    dnp3_response(rb, one(99, 1), bad_crc=(0,))                              # bad CRC: dropped
    dnp3_response(rb, one(98, 2))
    dnp3_response(rb, one(96, 3) + struct.pack("<BBBBBB", 99, 1, 0, 0, 0, 7) + one(97, 99))   # g99: size unknown
    dnp3_response(rb, struct.pack("<BBBBHI", 120, 1, 0x5B, 1, 4, 0) + one(95, 4))             # size-prefixed: skipped
    dnp3_response(rb, g40(1, 0x00, bytes([0, 39]), b"".join(struct.pack("<Bi", 1, 5) for _ in range(40))),
                  seg=60, skip=(1,))                                          # segment gap: dropped
    dnp3_response(rb, one(93, 6))
    dnp3_response(rb, g40(1, 0x00, bytes([80, 85]), struct.pack("<Bi", 1, 7) + struct.pack("<Bi", 1, 8)
                          + b"\x01\x00\x00"))                              # truncated: 2 whole items
    return pkts


def write_pcap(path, pkts):
    with open(path, "wb") as fh:
        fh.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        fh.write(b"".join(pkts))


def _rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.startswith("{")]


def check(logdir):
    rows = _rows(Path(logdir) / "dnp3_points.log")
    got_mb = [(r["func"], r["address"], r.get("request_values"), r.get("response_values"))
              for r in _rows(Path(logdir) / "modbus_detailed.log")
              if r.get("request_values") or r.get("response_values")]
    got_dn = [(r["group"], r["variation"], r["index"], r["value"], r["function_code"]) for r in rows]
    # Stock and Spicy rows interleave in event order, so compare as multisets.
    exp_dn = sorted(EXPECTED_DNP3)
    bad = [f"modbus: {got_mb}\n   != {EXPECTED_MODBUS}"] if got_mb != EXPECTED_MODBUS else []
    if sorted(got_dn) != exp_dn:
        bad.append(f"dnp3 missing={[x for x in exp_dn if x not in got_dn]} "
                   f"extra={[x for x in got_dn if x not in exp_dn]} (or a duplicate: {len(got_dn)} vs {len(exp_dn)} rows)")
    flags = {(r["group"], r["variation"], r["index"]): r.get("flags") for r in rows}
    wrong = {k: (flags.get(k), v) for k, v in EXPECTED_FLAGS.items() if flags.get(k) != v}
    if wrong:
        bad.append(f"dnp3 flags (got, expected): {wrong}")
    print("\n".join(bad) or f"OK: {len(got_mb)} modbus rows, {len(got_dn)} dnp3 points match")
    return 1 if bad else 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        sys.exit(check(sys.argv[2]))
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    write_pcap(sys.argv[1], build())
