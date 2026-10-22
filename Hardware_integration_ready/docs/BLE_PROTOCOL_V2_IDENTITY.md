# BLE Protocol V2 identity contract

Status: implemented in software and compile-verifiable firmware; physical BLE behavior remains unverified.

## Canonical identity

The immutable adapter identity is `UID64 = (uint64_t(DEVICEID[1]) << 32) | DEVICEID[0]`.
Every host-facing text representation is exactly 16 uppercase hexadecimal characters with leading zeroes.
`0000000000000000` and `FFFFFFFFFFFFFFFF` are invalid. The adapter contains no patient data.

## Advertising

The primary legacy advertising packet contains Flags followed by one 128-bit Service Data AD structure (`AD type 0x21`):

| Offset | Size | Field | Encoding |
|---:|---:|---|---|
| 0 | 16 | Service UUID | `6e4e0100-b5a3-f393-e0a9-e50e24dcca9e`, Bluetooth little-endian UUID wire order |
| 16 | 1 | protocol version | unsigned byte, exactly `2` |
| 17 | 8 | UID64 | unsigned 64-bit little-endian |

Bleak exposes the final nine bytes under `advertisement_data.service_data`, keyed by the canonical service UUID.
The parser requires exactly nine bytes, version 2, and a non-sentinel UID. Missing, short, long,
unsupported-version, or invalid values are ignored and never enter live discovery.

Legacy advertising budgets are fixed and independently test-checked:

- Flags: `2` AD-structure overhead bytes + `1` payload byte = `3` bytes.
- 128-bit Service Data: `2` overhead + `16` UUID + `1` version + `8` UID = `27` bytes.
- Primary advertisement total: `3 + 27 = 30` of the available `31` bytes.
- Scan-response local name `VeinNetSensorV2`: `2 + 15 = 17` of `31` bytes.

The primary advertisement deliberately has no Service UUID List AD structure. Adding a complete 128-bit UUID
list while retaining Flags and Service Data would require `3 + 18 + 27 = 48` bytes. Placing that UUID list
beside the current name in the scan response would require `18 + 17 = 35` bytes. Both exceed their respective
31-byte legacy limits. Splitting identity-dependent matching across advertisement and scan response would also
make discovery depend on platform scan-response merging.

Production discovery therefore performs a bounded platform scan without a platform-level service-UUID
prefilter, then admits only advertisements containing this exact service-data UUID and complete valid nine-byte
payload. This is required because CoreBluetooth evaluates a Service UUID List filter before it exposes Service
Data; BlueZ also defines UUID filtering independently from application inspection of ServiceData. Removing the
platform prefilter changes host discovery mechanics only: the V2 bytes, UUIDs, parser, and protocol version are
unchanged. Unrelated and V1 advertisements reach at most the local callback and are rejected there.

The BLE device/address object is retained only as an opaque source token and connection handle.
It is never a machine identity or nurse-facing value.

## Connected GATT identity

Service `6e4e0100-b5a3-f393-e0a9-e50e24dcca9e` exposes read-only characteristic
`6e4e0104-b5a3-f393-e0a9-e50e24dcca9e`. Its fixed 16-byte value is:

| Offset | Size | Field | Encoding |
|---:|---:|---|---|
| 0 | 1 | protocol version | exactly `2` |
| 1 | 1 | flags | exactly `0x01` (provisioned) |
| 2 | 8 | UID64 | unsigned 64-bit little-endian |
| 10 | 2 | short-code number | unsigned 16-bit little-endian; `1` means `VN-001`; zero invalid |
| 12 | 2 | reserved | exactly zero |
| 14 | 2 | CRC16-CCITT | little-endian; init `0xFFFF`, polynomial `0x1021`, covers bytes 0–13 |

Before assignment and before notifications/START on every reconnect, the Jetson reads this characteristic and
requires UID and short code to equal the advertised UID and current inventory record. Failure or mismatch
disconnects that path and leaves it untrusted. Mutable authorization, consumed, active-assignment, freshness,
and duplicate checks are repeated after the connected read before commit.

## Two-phase physical provisioning

`reserve_provisioning(uid)` atomically reserves the next sequential `VN-NNN` in
`PENDING_PROVISIONING`. That record is visible for recovery but is neither assignable nor auto-bindable.
Repeating a reservation for the same pending UID returns the same VN code. The per-unit production build is then
created with `scripts/build_provisioned_firmware.py`; only the numeric VN suffix is compiled in, while UID always
comes from FICR at runtime. `finalize_provisioning(...)` changes the state to `PROVISIONED` only when the production
GATT value reports exactly the reserved UID and VN code. An interrupted attempt stays pending or is explicitly
retired; codes are never silently reused.

The generic sketch default is `VEINNET_SHORT_CODE_NUMBER=0`. Its GATT flags are unprovisioned, and it is never
eligible for production assignment or a final per-unit restore.

## Sensor-independent identity startup

FICR identity, the V2 service, GATT identity, and advertising initialize before MAX30102 probing. When the sensor
is absent, identity remains discoverable/readable, `sensor_config_id=0` explicitly means sensor unavailable or
unverified, FIFO pointers are zero, and START cannot enable acquisition or DATA notifications. No PPG, HR, SpO2,
or BP is fabricated. With a verified sensor, the frozen 400-sps/average-4/effective-100-Hz path is unchanged.

## DATA, STATUS, CONTROL and UUID versioning

V2 uses the `6e4e01xx-...` UUID family, distinct from V1 `6e4e00xx-...`.
DATA and STATUS replace V1's `uint16 device_id` with UID64 and carry protocol version 2. DATA header is 33 bytes
and permits at most 18 RED/IR pairs, keeping the largest notification below the existing ATT payload assumption.
STATUS is 29 bytes. CONTROL remains 14 bytes but requires version 2. All multibyte values are little-endian and
packet CRC rules are unchanged.

The sensor acquisition contract is unchanged: MAX30102 configured at 400 sps, average 4, effective paired RED/IR
output 100 Hz. Model, preprocessing, morphology, schemas, and patient workflow contracts are unchanged.

## Legacy V1 behavior

V1 UUIDs and packets are not accepted by the production V2 central. V1 fixtures may be retained only in isolated
legacy tests/tools. A V1 device has no verifiable immutable identity and can never enter live V2 discovery, pass
the GATT identity check, or become eligible for production patient assignment. There is no fallback to name,
address, RSSI, or order.

## Security boundary

Freshness, simultaneous duplicate-source detection, and connected identity consistency protect against ambiguity
and ordinary stale/mismatched state. They do not authenticate firmware or resist an active radio spoofing/relay
attacker. Cryptographic device authentication is intentionally outside Stage 4; an unauthenticated nonce or
counter would not solve that threat.
