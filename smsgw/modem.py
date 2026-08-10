"""Inbound SMS.

Polls the modem's SIM storage rather than relying on +CMTI unsolicited codes: a
URC that arrives while the port is mid-send is lost, and a missed inbound message
is invisible. Polling costs one AT command every few seconds and cannot miss.

Shares the send sink's lock, because an AT port is single-writer - a poll landing
in the middle of a +CMGS prompt breaks both operations.
"""
from __future__ import annotations

import logging
import re
import threading
import time

log = logging.getLogger(__name__)

# One lock per serial port, shared by everything that touches it: the SMS sink,
# the MMS sink, the inbound poller and the health probe. An AT port is
# single-writer, and a third sink made per-object locks untenable - a health
# check landing between AT+CMGS and its '>' prompt corrupts both operations.
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def port_lock(port: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(port, threading.RLock())

# +CMGL: 1,"REC UNREAD","+15551234567",,"26/08/09,14:22:31-24"
_HEADER = re.compile(
    r'\+CMGL:\s*(\d+),"([^"]*)","([^"]*)",[^,]*,"([^"]*)"'
)


class ModemInbox:
    def __init__(self, sink, on_message):
        self.sink = sink          # SMSModemSink - owns the port and the lock
        self.on_message = on_message

    def _cmd(self, ser, cmd: str, wait: float = 1.0) -> str:
        ser.reset_input_buffer()
        ser.write((cmd + "\r").encode())
        out, deadline = b"", time.time() + wait + 4
        while time.time() < deadline:
            chunk = ser.read(4096)
            if chunk:
                out += chunk
                if out.rstrip().endswith(b"OK") or b"ERROR" in out:
                    break
                deadline = time.time() + 0.4
            else:
                time.sleep(0.05)
        return out.decode(errors="replace")

    def poll_once(self) -> int:
        """Read and delete any stored messages. Returns how many were handled."""
        import serial

        handled = 0
        with self.sink._lock:
            try:
                ser = serial.Serial(self.sink.port, self.sink.baud, timeout=1)
            except Exception as e:
                log.debug("inbox: cannot open %s: %s", self.sink.port, e)
                return 0
            try:
                if "OK" not in self._cmd(ser, "AT"):
                    return 0
                self._cmd(ser, "AT+CMGF=1")
                self._cmd(ser, 'AT+CPMS="SM","SM","SM"')
                listing = self._cmd(ser, 'AT+CMGL="ALL"', wait=3)

                for index, status, sender, stamp, body in self._parse(listing):
                    try:
                        self.on_message(sender, body, stamp)
                        handled += 1
                    except Exception:
                        log.exception("inbox: handler failed for index %s", index)
                        continue  # leave it on the SIM so the next poll retries
                    # Only delete once the handler has durably stored it.
                    self._cmd(ser, f"AT+CMGD={index}")
            finally:
                ser.close()
        return handled

    @staticmethod
    def _parse(listing: str):
        """Yield (index, status, sender, timestamp, body) from a +CMGL dump."""
        lines = listing.splitlines()
        out = []
        i = 0
        while i < len(lines):
            m = _HEADER.search(lines[i])
            if not m:
                i += 1
                continue
            index, status, sender, stamp = m.groups()
            # The body runs until the next +CMGL header or the trailing OK.
            body_lines = []
            i += 1
            while i < len(lines):
                nxt = lines[i].strip()
                if _HEADER.search(lines[i]) or nxt == "OK":
                    break
                body_lines.append(lines[i])
                i += 1
            out.append((int(index), status, sender, stamp,
                        "\n".join(body_lines).strip()))
        return out


def modem_status(port: str, baud: int = 115200, lock=None) -> dict:
    """One-shot health probe: registration, signal, SIM state.

    Defaults to the shared port lock - /health is a writer like any other.
    """
    import serial

    fields = {}
    lock = lock or port_lock(port)
    try:
        with lock, serial.Serial(port, baud, timeout=1) as ser:
            for cmd, key in (("AT+CPIN?", "sim"), ("AT+CSQ", "signal"),
                             ("AT+COPS?", "operator"), ("AT+CREG?", "creg"),
                             ("AT+CEREG?", "cereg")):
                ser.reset_input_buffer()
                ser.write((cmd + "\r").encode())
                time.sleep(0.4)
                raw = ser.read(512).decode(errors="replace")
                for line in raw.splitlines():
                    line = line.strip()
                    if line.startswith("+"):
                        fields[key] = line
                        break
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    out = {"ok": bool(fields), "raw": fields}
    m = re.search(r"\+CSQ:\s*(\d+)", fields.get("signal", ""))
    if m and int(m.group(1)) != 99:
        # +CSQ maps 0-31 onto -113..-51 dBm in 2 dBm steps.
        out["rssi_dbm"] = -113 + 2 * int(m.group(1))
    m = re.search(r'\+COPS:\s*\d+,\d+,"([^"]*)"', fields.get("operator", ""))
    if m:
        out["operator"] = m.group(1)
    m = re.search(r"\+CEREG:\s*\d+,(\d+)", fields.get("cereg", ""))
    if m:
        out["registered"] = m.group(1) in ("1", "5")
    return out
