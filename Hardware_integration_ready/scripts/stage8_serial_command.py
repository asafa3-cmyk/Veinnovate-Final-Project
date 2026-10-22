#!/usr/bin/env python3
"""Send one bounded command to the Stage 8 probe over a local serial TTY."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import termios
import time


TERMINAL_EVENT = {"IDENTIFY": "identity", "START_FIFO": "capture_complete", "STOP": "stopped"}
TIMEOUT_S = {"IDENTIFY": 5.0, "START_FIFO": 15.0, "STOP": 5.0}


def validate_transcript(command: str, records: list[dict]) -> dict:
    errors = [record for record in records if record.get("event") == "error"]
    if errors:
        raise RuntimeError(f"probe failed closed: {errors[-1].get('code', 'UNKNOWN')}")
    terminal = [record for record in records if record.get("event") == TERMINAL_EVENT[command]]
    if len(terminal) != 1:
        raise RuntimeError(f"expected exactly one {TERMINAL_EVENT[command]} event")
    result = terminal[0]
    if command == "IDENTIFY":
        if result.get("led_drive_enabled") is not False:
            raise RuntimeError("identity response did not prove LED drive disabled")
    elif command == "START_FIFO":
        samples = [record for record in records if record.get("event") == "sample"]
        if result.get("samples") != len(samples):
            raise RuntimeError("capture summary/sample count mismatch")
        if result.get("led_drive_enabled") is not False:
            raise RuntimeError("capture did not finish with LED drive disabled")
    return result


def _configure_serial(fd: int) -> None:
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0
    attrs[4] = termios.B115200
    attrs[5] = termios.B115200
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIFLUSH)


def run(port: Path, command: str) -> list[dict]:
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        _configure_serial(fd)
        os.write(fd, f"{command}\n".encode("ascii"))
        deadline = time.monotonic() + TIMEOUT_S[command]
        buffer = bytearray()
        records: list[dict] = []
        while time.monotonic() < deadline:
            readable, _, _ = select.select([fd], [], [], min(0.2, deadline - time.monotonic()))
            if not readable:
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                continue
            buffer.extend(chunk)
            while b"\n" in buffer:
                raw, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                if not raw.strip():
                    continue
                record = json.loads(raw.decode("utf-8"))
                records.append(record)
                if record.get("event") in {TERMINAL_EVENT[command], "error"}:
                    validate_transcript(command, records)
                    return records
        raise TimeoutError(f"no terminal response within {TIMEOUT_S[command]:.0f} seconds")
    finally:
        os.close(fd)


def _atomic_write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records)
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=Path, required=True)
    parser.add_argument("--command", choices=sorted(TERMINAL_EVENT), required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    records = run(args.port, args.command)
    if args.output:
        _atomic_write(args.output, records)
    print(json.dumps(validate_transcript(args.command, records), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
