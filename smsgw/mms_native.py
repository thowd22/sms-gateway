"""Native MMS over the cellular link.

The SIM7600 has no MMS client (`AT+CLAC` lists zero `CMMS*` commands), so we
build the `M-Send.req` PDU ourselves and POST it to the carrier's MMSC through
the modem's own TCP stack.

Why the modem's stack and not the Pi's: the MMSC lives on a carrier-internal
address (10.177.171.x) reachable only from inside the mobile network. Routing
that from the Pi would mean bringing up wwan0, and this modem's QMI WDS service
refuses to allocate a client (DMS works, WDS times out). The modem's own stack
is already bound to the PDP context, so it sidesteps the problem entirely.

Payload goes out in <=1500-byte chunks because that is the AT+CIPSEND ceiling.
"""
from __future__ import annotations

import logging
import re
import sys
import time

from .modem import port_lock

log = logging.getLogger(__name__)

MMSC_HOST = "mms.msg.eng.t-mobile.com"
MMSC_PATH = "/mms/wapenc"
MMSC_PORT = 80
APN = "fast.t-mobile.com"
CID = 5                     # spare context; 1-4 are the network's own
CHUNK = 1400                # under the 1500 AT+CIPSEND ceiling, with headroom

sys.path.insert(0, "/opt/smsgw/vendor")


class MMSError(Exception):
    pass


def build_pdu(to: str, sender: str, text: str, image_path: str | None = None,
              subject: str | None = None, transaction_id: str | None = None) -> bytes:
    """Encode an M-Send.req. Addresses take the /TYPE=PLMN suffix."""
    import uuid

    from messaging.mms.message import MMSMessage, MMSMessagePage

    m = MMSMessage()
    m.headers["Message-Type"] = "m-send-req"
    m.headers["MMS-Version"] = "1.2"
    # Must be unique per message. The library defaults to a literal "1234",
    # which invites the MMSC to treat a retry as a duplicate of the original.
    m.headers["Transaction-Id"] = transaction_id or uuid.uuid4().hex[:16]
    m.headers["To"] = f"{to}/TYPE=PLMN"
    m.headers["From"] = f"{sender}/TYPE=PLMN"
    if subject:
        m.headers["Subject"] = subject

    page = MMSMessagePage()
    if image_path:
        page.add_image(image_path)
    if text:
        page.add_text(text)
    m.add_page(page)
    return bytes(m.encode())


class ModemHTTP:
    """Minimal HTTP-over-AT client bound to a dedicated PDP context."""

    def __init__(self, port: str = "/dev/sim7600-at", baud: int = 115200):
        self.port, self.baud = port, baud
        self.ser = None
        self._lock = port_lock(port)

    def __enter__(self):
        import serial
        # Held for the whole exchange: a 15KB POST is ~12 chunked AT writes and
        # must not interleave with an SMS send or an inbox poll.
        self._lock.acquire()
        self.ser = serial.Serial(self.port, self.baud, timeout=1)
        return self

    def __exit__(self, *exc):
        try:
            self.cmd("AT+CIPCLOSE=0", 4)
            self.cmd("AT+NETCLOSE", 6)
            self.cmd(f"AT+CGACT=0,{CID}", 5)
            self.cmd(f"AT+CGDCONT={CID}", 2)
        finally:
            self.ser.close()
            self._lock.release()

    def cmd(self, c: str, wait: float = 1.0) -> str:
        self.ser.reset_input_buffer()
        self.ser.write((c + "\r").encode())
        time.sleep(min(wait, 0.5))
        out, dl = b"", time.time() + wait + 6
        while time.time() < dl:
            ch = self.ser.read(4096)
            if ch:
                out += ch
                if out.rstrip().endswith(b"OK") or b"ERROR" in out:
                    break
                dl = time.time() + 0.4
            else:
                time.sleep(0.05)
        return out.decode(errors="replace")

    def connect(self) -> None:
        self.cmd(f'AT+CGDCONT={CID},"IP","{APN}"')
        if "OK" not in self.cmd(f"AT+CGACT=1,{CID}", 12):
            raise MMSError("could not activate the IPv4 PDP context")
        self.cmd(f'AT+CGSOCKCONT={CID},"IP","{APN}"')
        self.cmd(f"AT+CSOCKSETPN={CID}")
        self.cmd("AT+CIPRXGET=1")               # manual receive
        self.cmd("AT+NETOPEN", 12)

        self.ser.reset_input_buffer()
        self.ser.write(f'AT+CIPOPEN=0,"TCP","{MMSC_HOST}",{MMSC_PORT}\r'.encode())
        out, dl = b"", time.time() + 40
        while time.time() < dl:
            ch = self.ser.read(4096)
            if ch:
                out += ch
                if b"+CIPOPEN:" in out:
                    break
            else:
                time.sleep(0.1)
        if b"+CIPOPEN: 0,0" not in out:
            raise MMSError(f"TCP connect to the MMSC failed: {out!r}")

    def _send_chunk(self, data: bytes) -> None:
        self.ser.reset_input_buffer()
        self.ser.write(f"AT+CIPSEND=0,{len(data)}\r".encode())
        # Wait for the '>' data prompt before writing the payload.
        prompt, dl = b"", time.time() + 10
        while time.time() < dl:
            ch = self.ser.read(64)
            if ch:
                prompt += ch
                if b">" in prompt:
                    break
            else:
                time.sleep(0.02)
        if b">" not in prompt:
            raise MMSError(f"no send prompt (got {prompt!r})")

        self.ser.write(data)
        ack, dl = b"", time.time() + 30
        while time.time() < dl:
            ch = self.ser.read(256)
            if ch:
                ack += ch
                if b"OK" in ack or b"ERROR" in ack:
                    break
            else:
                time.sleep(0.05)
        if b"ERROR" in ack or b"OK" not in ack:
            raise MMSError(f"chunk rejected: {ack!r}")

    def post(self, pdu: bytes) -> bytes:
        head = (
            f"POST {MMSC_PATH} HTTP/1.1\r\n"
            f"Host: {MMSC_HOST}\r\n"
            f"Content-Type: application/vnd.wap.mms-message\r\n"
            f"Content-Length: {len(pdu)}\r\n"
            f"Accept: */*\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
        payload = head + pdu

        total = (len(payload) + CHUNK - 1) // CHUNK
        for i in range(0, len(payload), CHUNK):
            self._send_chunk(payload[i:i + CHUNK])
            log.info("sent chunk %d/%d", i // CHUNK + 1, total)

        return self._read_response()

    def _read_response(self, timeout: float = 45) -> bytes:
        body, dl = b"", time.time() + timeout
        while time.time() < dl:
            got = self.cmd("AT+CIPRXGET=2,0,1500", 2)
            m = re.search(r"\+CIPRXGET: 2,0,(\d+),(\d+)", got)
            if m and int(m.group(1)) > 0:
                start = got.index(m.group(0)) + len(m.group(0))
                chunk = got[start:].lstrip("\r\n")
                if chunk.endswith("OK\r\n"):
                    chunk = chunk[: -len("OK\r\n")]
                body += chunk.encode("latin-1", "replace")
                dl = time.time() + 5
                continue
            if body:
                break
            time.sleep(1)
        return body


def interpret(response: bytes) -> dict:
    """Decide whether the MMSC accepted the submission.

    The library's decoder mis-parses m-send-conf (it reports the type as
    m-send-req and garbles the header names), so acceptance is read from the
    raw response instead: the MMSC emits a "<code>:<text>" status and, on
    success, a UUID Message-ID. Both are unambiguous in the bytes.
    """
    out: dict = {"raw_len": len(response), "accepted": False}
    if not response:
        out["error"] = "empty response from the MMSC"
        return out

    text = response.decode("latin-1", "replace")
    out["status_line"] = text.split("\r\n", 1)[0]
    http_ok = out["status_line"].startswith("HTTP/1.") and " 200 " in out["status_line"]

    body = response.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in response else b""
    out["body_len"] = len(body)
    btext = body.decode("latin-1", "replace")

    m = re.search(r"(\d{4}):([A-Za-z ]+)", btext)
    if m:
        out["mmsc_status"] = f"{m.group(1)}:{m.group(2)}"
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                  btext)
    if m:
        out["message_id"] = m.group(0)

    # A Message-ID means the carrier took custody; that is the real signal.
    # HTTP 200 alone is not enough - the MMSC returns 200 with an error PDU.
    out["accepted"] = bool(http_ok and out.get("message_id"))
    return out
