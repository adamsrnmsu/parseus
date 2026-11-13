##! Parseus ICS deep packet inspection.
##!
##! Adds two JSON logs on top of Zeek's stock modbus.log and dnp3.log, using the
##! events the stock Modbus and DNP3 analyzers already raise plus one event from the
##! Parseus Spicy analyzer (spicy/parseus_dnp3.spicy) for what stock DNP3 misses:
##!   modbus_detailed.log  one row per Modbus request/response with register values
##!   dnp3_points.log      one row per DNP3 point value (binary, counter, analog,
##!                        analog output status g40, binary input events g2)

@load base/protocols/modbus
@load base/protocols/dnp3

redef LogAscii::use_json = T;
# Rotate hourly so the log directory stays bounded; the Parseus ingestor follows
# rotated files by inode, so nothing is lost at the boundary.
redef Log::default_rotation_interval = 1 hr;

module ParseusICS;

export {
	redef enum Log::ID += { MODBUS_LOG, DNP3_LOG };

	type Modbus: record {
		ts:              time           &log;
		uid:             string         &log;
		id:              conn_id        &log;
		is_orig:         bool           &log;
		tid:             count          &log;
		unit:            count          &log;
		func:            string         &log;
		address:         count          &log &optional;
		quantity:        count          &log &optional;
		request_values:  vector of count &log &optional;
		response_values: vector of count &log &optional;
	};

	type DNP3Point: record {
		ts:            time   &log;
		uid:           string &log;
		id:            conn_id &log;
		is_orig:       bool   &log;
		function_code: string &log;
		group:         count  &log;
		object_type:   string &log;
		variation:     count  &log;
		index:         count  &log;
		value:         double &log;
		flags:         count  &log &optional;
	};
}

# ---------------------------------------------------------------- Modbus

# Responses carry no address, so remember the request's start address by transaction.
global mb_addr: table[string] of count &create_expire=1 min;
global mb_qty: table[string] of count &create_expire=1 min;

function mb_key(c: connection, h: ModbusHeaders): string
	{
	return fmt("%s/%d/%d", c$uid, h$tid, h$uid);
	}

function mb_bits(coils: ModbusCoils): vector of count
	{
	local v: vector of count;
	for ( i in coils )
		v += coils[i] ? 1 : 0;
	return v;
	}

function mb_log(c: connection, h: ModbusHeaders, is_orig: bool, address: count, quantity: count, values: vector of count, is_request: bool)
	{
	local r = Modbus($ts=network_time(), $uid=c$uid, $id=c$id, $is_orig=is_orig,
	                 $tid=h$tid, $unit=h$uid, $func=Modbus::function_codes[h$function_code],
	                 $address=address);
	if ( quantity > 0 )
		r$quantity = quantity;
	if ( |values| > 0 )
		{
		if ( is_request )
			r$request_values = values;
		else
			r$response_values = values;
		}
	Log::write(MODBUS_LOG, r);
	}

function mb_read_request(c: connection, h: ModbusHeaders, start: count, quantity: count)
	{
	mb_addr[mb_key(c, h)] = start;
	mb_qty[mb_key(c, h)] = quantity;
	mb_log(c, h, T, start, quantity, vector(), T);
	}

function mb_read_response(c: connection, h: ModbusHeaders, values: vector of count)
	{
	local k = mb_key(c, h);
	if ( k !in mb_addr )
		return;   # request not seen: without an address the values are not usable
	# Coil responses are padded to whole bytes; keep only the requested quantity.
	local n = mb_qty[k];
	local v: vector of count;
	for ( i in values )
		if ( i < n )
			v += values[i];
	mb_log(c, h, F, mb_addr[k], |v|, v, F);
	delete mb_addr[k];
	delete mb_qty[k];
	}

event modbus_read_coils_request(c: connection, headers: ModbusHeaders, start_address: count, quantity: count)
	{ mb_read_request(c, headers, start_address, quantity); }
event modbus_read_coils_response(c: connection, headers: ModbusHeaders, coils: ModbusCoils)
	{ mb_read_response(c, headers, mb_bits(coils)); }
event modbus_read_discrete_inputs_request(c: connection, headers: ModbusHeaders, start_address: count, quantity: count)
	{ mb_read_request(c, headers, start_address, quantity); }
event modbus_read_discrete_inputs_response(c: connection, headers: ModbusHeaders, coils: ModbusCoils)
	{ mb_read_response(c, headers, mb_bits(coils)); }
event modbus_read_holding_registers_request(c: connection, headers: ModbusHeaders, start_address: count, quantity: count)
	{ mb_read_request(c, headers, start_address, quantity); }
event modbus_read_holding_registers_response(c: connection, headers: ModbusHeaders, registers: ModbusRegisters)
	{ mb_read_response(c, headers, registers); }
event modbus_read_input_registers_request(c: connection, headers: ModbusHeaders, start_address: count, quantity: count)
	{ mb_read_request(c, headers, start_address, quantity); }
event modbus_read_input_registers_response(c: connection, headers: ModbusHeaders, registers: ModbusRegisters)
	{ mb_read_response(c, headers, registers); }

event modbus_write_single_coil_request(c: connection, headers: ModbusHeaders, address: count, value: bool)
	{ mb_log(c, headers, T, address, 1, vector(value ? 1 : 0), T); }
event modbus_write_single_register_request(c: connection, headers: ModbusHeaders, address: count, value: count)
	{ mb_log(c, headers, T, address, 1, vector(value), T); }
event modbus_write_multiple_coils_request(c: connection, headers: ModbusHeaders, start_address: count, coils: ModbusCoils)
	{ mb_log(c, headers, T, start_address, |coils|, mb_bits(coils), T); }
event modbus_write_multiple_registers_request(c: connection, headers: ModbusHeaders, start_address: count, registers: ModbusRegisters)
	{ mb_log(c, headers, T, start_address, |registers|, registers, T); }

# ------------------------------------------------------------------ DNP3

type Cursor: record {
	fc:     string &default="";
	obj:    count  &default=0;   # group * 256 + variation
	qua:    count  &default=0;
	first:  count  &default=0;   # start index for range qualifiers
	n:      count  &default=0;   # items announced by the object header
	k:      count  &default=0;   # items emitted so far
	prefix: count  &default=0;   # index prefix for prefixed qualifiers
};

global cur: table[string] of Cursor &create_expire=5 min;

function cursor(c: connection): Cursor
	{
	if ( c$uid !in cur )
		cur[c$uid] = Cursor();
	return cur[c$uid];
	}

function obj_name(group: count): string
	{
	switch ( group ) {
	case 1, 2, 3, 4: return "binary_input";
	case 10, 11:     return "binary_output";
	case 20, 22:     return "counter";
	case 21, 23:     return "frozen_counter";
	case 30, 32:     return "analog_input";
	case 31, 33:     return "frozen_analog_input";
	case 40, 42:     return "analog_output_status";
	}
	return "other";
	}

function pow2(n: count): double
	{
	local r = 1.0;
	while ( n > 0 )
		{ r = r * 2.0; --n; }
	return r;
	}

# Zeek hands DNP3 analog values over as unsigned counts.
function signed(x: count, bits: count): double
	{
	local d = count_to_double(x);
	return (x >> (bits - 1)) == 1 ? d - pow2(bits) : d;
	}
function s16(x: count): double
	{ return signed(x & 0xffff, 16); }
function s32(x: count): double
	{ return signed(x & 0xffffffff, 32); }
function f32(b: count): double
	{
	local e = (b >> 23) & 0xff;
	local m = b & 0x7fffff;
	local s = (b >> 31) == 1 ? -1.0 : 1.0;
	if ( e == 0 )
		return s * count_to_double(m) / pow2(149);
	if ( e < 127 )
		return s * (1.0 + count_to_double(m) / 8388608.0) / pow2(127 - e);
	return s * (1.0 + count_to_double(m) / 8388608.0) * pow2(e - 127);
	}

function point(c: connection, is_orig: bool, value: double, flags: count)
	{
	local s = cursor(c);
	local qh = s$qua / 16;
	local idx = (qh >= 1 && qh <= 3) ? s$prefix : s$first + s$k;
	++s$k;
	Log::write(DNP3_LOG, DNP3Point($ts=network_time(), $uid=c$uid, $id=c$id, $is_orig=is_orig,
	           $function_code=s$fc, $group=s$obj / 256, $object_type=obj_name(s$obj / 256),
	           $variation=s$obj % 256, $index=idx, $value=value, $flags=flags));
	}

# IEEE 754 binary64 from the two little-endian 32-bit words Zeek hands over
# (value_low = first four bytes on the wire).
function f64(lo: count, hi: count): double
	{
	local e = (hi >> 20) & 0x7ff;
	local m = count_to_double(hi & 0xfffff) * 4294967296.0 + count_to_double(lo);
	local s = (hi >> 31) == 1 ? -1.0 : 1.0;
	if ( e == 0 )
		return s * m / pow2(1074);
	if ( e < 1023 )
		return s * (1.0 + m / pow2(52)) / pow2(1023 - e);
	return s * (1.0 + m / pow2(52)) * pow2(e - 1023);
	}

# Doubles with exponent 2047 (inf/NaN) are skipped the same way as floats.
function point_dp(c: connection, is_orig: bool, lo: count, hi: count, flags: count)
	{
	if ( ((hi >> 20) & 0x7ff) == 2047 )
		++cursor(c)$k;
	else
		point(c, is_orig, f64(lo, hi), flags);
	}

# IEEE inf/NaN (exponent 255) is not a usable value: skip it but keep the index count.
function point_sp(c: connection, is_orig: bool, bits: count, flags: count)
	{
	if ( ((bits >> 23) & 0xff) == 255 )
		++cursor(c)$k;
	else
		point(c, is_orig, f32(bits), flags);
	}

event dnp3_application_request_header(c: connection, is_orig: bool, application: count, fc: count)
	{ cursor(c)$fc = DNP3::function_codes[fc]; }
event dnp3_application_response_header(c: connection, is_orig: bool, application: count, fc: count, iin: count)
	{ cursor(c)$fc = DNP3::function_codes[fc]; }

event dnp3_object_header(c: connection, is_orig: bool, obj_type: count, qua_field: count, number: count, rf_low: count, rf_high: count)
	{
	local s = cursor(c);
	s$obj = obj_type; s$qua = qua_field; s$n = number; s$k = 0; s$prefix = 0;
	# Start/stop range qualifiers 0x00-0x02 (1, 2, 4 byte) carry a start index.
	s$first = qua_field <= 0x02 ? rf_low : 0;
	}

# Raised for every object, including range qualifiers (then the value is 0); the
# prefix is only an index for the prefixed qualifiers, which point() checks.
event dnp3_object_prefix(c: connection, is_orig: bool, prefix_value: count)
	{ cursor(c)$prefix = prefix_value; }

# Binary objects: g1v1/g10v1 pack 8 points per byte, g1v2/g10v2 are one flag byte each.
# g2 (binary input event) is not here: stock Zeek raises no event carrying its flag
# byte. The Spicy analyzer's parseus_dnp3_point covers it (and g40) instead.
event dnp3_response_data_object(c: connection, is_orig: bool, data_value: count)
	{
	local s = cursor(c);
	if ( s$obj == 0x0101 || s$obj == 0x0a01 )
		{
		local bit = 0;
		while ( bit < 8 && s$k < s$n )
			{
			point(c, is_orig, count_to_double((data_value >> bit) & 1), 0);
			++bit;
			}
		}
	else if ( s$obj == 0x0102 || s$obj == 0x0a02 )
		point(c, is_orig, count_to_double((data_value >> 7) & 1), data_value);
	}

event dnp3_counter_32wFlag(c: connection, is_orig: bool, flag: count, count_value: count)
	{ point(c, is_orig, count_to_double(count_value), flag); }
event dnp3_counter_16wFlag(c: connection, is_orig: bool, flag: count, count_value: count)
	{ point(c, is_orig, count_to_double(count_value), flag); }
event dnp3_counter_32woFlag(c: connection, is_orig: bool, count_value: count)
	{ point(c, is_orig, count_to_double(count_value), 0); }
event dnp3_counter_16woFlag(c: connection, is_orig: bool, count_value: count)
	{ point(c, is_orig, count_to_double(count_value), 0); }
event dnp3_frozen_counter_32wFlag(c: connection, is_orig: bool, flag: count, count_value: count)
	{ point(c, is_orig, count_to_double(count_value), flag); }
event dnp3_frozen_counter_16wFlag(c: connection, is_orig: bool, flag: count, count_value: count)
	{ point(c, is_orig, count_to_double(count_value), flag); }
event dnp3_frozen_counter_32woFlag(c: connection, is_orig: bool, count_value: count)
	{ point(c, is_orig, count_to_double(count_value), 0); }
event dnp3_frozen_counter_16woFlag(c: connection, is_orig: bool, count_value: count)
	{ point(c, is_orig, count_to_double(count_value), 0); }

event dnp3_analog_input_32wFlag(c: connection, is_orig: bool, flag: count, value: count)
	{ point(c, is_orig, s32(value), flag); }
event dnp3_analog_input_16wFlag(c: connection, is_orig: bool, flag: count, value: count)
	{ point(c, is_orig, s16(value), flag); }
event dnp3_analog_input_32woFlag(c: connection, is_orig: bool, value: count)
	{ point(c, is_orig, s32(value), 0); }
event dnp3_analog_input_16woFlag(c: connection, is_orig: bool, value: count)
	{ point(c, is_orig, s16(value), 0); }
event dnp3_analog_input_SPwFlag(c: connection, is_orig: bool, flag: count, value: count)
	{ point_sp(c, is_orig, value, flag); }
event dnp3_analog_input_event_32woTime(c: connection, is_orig: bool, flag: count, value: count)
	{ point(c, is_orig, s32(value), flag); }
event dnp3_analog_input_event_16woTime(c: connection, is_orig: bool, flag: count, value: count)
	{ point(c, is_orig, s16(value), flag); }
event dnp3_analog_input_event_32wTime(c: connection, is_orig: bool, flag: count, value: count, time48: count)
	{ point(c, is_orig, s32(value), flag); }
event dnp3_analog_input_event_16wTime(c: connection, is_orig: bool, flag: count, value: count, time48: count)
	{ point(c, is_orig, s16(value), flag); }
event dnp3_analog_input_event_SPwoTime(c: connection, is_orig: bool, flag: count, value: count)
	{ point_sp(c, is_orig, value, flag); }

event dnp3_analog_input_DPwFlag(c: connection, is_orig: bool, flag: count, value_low: count, value_high: count)
	{ point_dp(c, is_orig, value_low, value_high, flag); }
event dnp3_frozen_analog_input_DPwFlag(c: connection, is_orig: bool, flag: count, frozen_value_low: count, frozen_value_high: count)
	{ point_dp(c, is_orig, frozen_value_low, frozen_value_high, flag); }
event dnp3_analog_input_event_DPwoTime(c: connection, is_orig: bool, flag: count, value_low: count, value_high: count)
	{ point_dp(c, is_orig, value_low, value_high, flag); }
event dnp3_analog_input_event_DPwTime(c: connection, is_orig: bool, flag: count, value_low: count, value_high: count, time48: count)
	{ point_dp(c, is_orig, value_low, value_high, flag); }
event dnp3_frozen_analog_input_event_DPwoTime(c: connection, is_orig: bool, flag: count, frozen_value_low: count, frozen_value_high: count)
	{ point_dp(c, is_orig, frozen_value_low, frozen_value_high, flag); }
event dnp3_frozen_analog_input_event_DPwTime(c: connection, is_orig: bool, flag: count, frozen_value_low: count, frozen_value_high: count, time48: count)
	{ point_dp(c, is_orig, frozen_value_low, frozen_value_high, flag); }

# From the Spicy analyzer, which runs next to the stock one on the same connection and
# only emits objects the stock analyzer has no event for (g40v1-v4, g2v1-v3), so
# nothing is logged twice. Index, value (inf/NaN already dropped) and flags are final.
event ParseusDNP3::point(c: connection, is_orig: bool, fc: count, group: count, variation: count, index: count, value: double, flags: count)
	{
	Log::write(DNP3_LOG, DNP3Point($ts=network_time(), $uid=c$uid, $id=c$id, $is_orig=is_orig,
	           $function_code=DNP3::function_codes[fc], $group=group, $object_type=obj_name(group),
	           $variation=variation, $index=index, $value=value, $flags=flags));
	}

event connection_state_remove(c: connection)
	{
	delete cur[c$uid];
	}

event zeek_init()
	{
	Log::create_stream(MODBUS_LOG, [$columns=Modbus, $path="modbus_detailed"]);
	Log::create_stream(DNP3_LOG, [$columns=DNP3Point, $path="dnp3_points"]);
	}
