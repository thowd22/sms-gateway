"""Delivery sinks.

Every sink exposes send(recipient_contact, body, image_path) -> None and raises on
failure. The rest of the system does not know or care how a message leaves the box,
which is what lets a native-MMS implementation drop in later without touching
sources or rules.
"""
from __future__ import annotations

import io
import logging
import smtplib
import threading
import time
import unicodedata
from email.message import EmailMessage
from pathlib import Path

from .config import ConfigError, read_secret
from .modem import port_lock

log = logging.getLogger(__name__)


class SendError(Exception):
    pass


# The GSM 03.38 default alphabet. Anything outside it forces a message to UCS-2,
# which cuts a segment from 160 characters to 70 and can double the cost.
GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅå_ÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
    "ΔΦΓΛΩΠΨΣΘΞ€[]{}\\~^|"
)

_TRANSLIT = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ",
}


def to_gsm7(text: str) -> str:
    """Force text into the GSM-7 alphabet, transliterating what does not fit."""
    out = []
    for ch in text:
        if ch in GSM7:
            out.append(ch)
            continue
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
            continue
        # Strip accents and retry; drop anything still unrepresentable (emoji).
        folded = unicodedata.normalize("NFKD", ch)
        kept = "".join(c for c in folded if c in GSM7)
        out.append(kept)
    return "".join(out)


def resize_jpeg(data: bytes, max_width: int, quality: int) -> bytes:
    """Shrink a snapshot so it stays under carrier MMS size limits."""
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if img.width > max_width:
        h = round(img.height * max_width / img.width)
        img = img.resize((max_width, h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


class EmailMMSSink:
    """Deliver text + picture through a carrier's email-to-MMS gateway.

    Free and needs no SIM, but it rides the internet rather than the modem - so it
    cannot survive the outage a cellular gateway exists for. That tradeoff is the
    reason sinks are pluggable.
    """

    type = "email_mms"

    def __init__(self, spec: dict):
        self.name = spec.get("name", "mms")
        smtp = spec.get("smtp") or {}
        self.host = smtp.get("host")
        self.port = int(smtp.get("port", 587))
        self.user = smtp.get("user")
        if not self.host or not self.user:
            raise ConfigError(f"sink {self.name}: smtp.host and smtp.user are required")
        pw_file = smtp.get("password_file")
        if not pw_file:
            raise ConfigError(f"sink {self.name}: smtp.password_file is required")
        self._pw_file = pw_file
        self.sender = spec.get("from") or self.user
        img = spec.get("image") or {}
        self.max_width = int(img.get("max_width", 640))
        self.quality = int(img.get("quality", 70))
        self.timeout = int(smtp.get("timeout", 30))

    def send(self, to, body: str, image_path: str | None = None) -> None:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to.mms_address()
        # Carrier gateways put the subject in the message body; leaving it empty
        # avoids a duplicated first line on the handset.
        msg["Subject"] = ""
        msg.set_content(body)

        if image_path:
            p = Path(image_path)
            if p.is_file():
                data = p.read_bytes()
                try:
                    data = resize_jpeg(data, self.max_width, self.quality)
                except Exception as e:  # a bad frame must not block the alert
                    log.warning("could not resize %s (%s); sending original", p, e)
                msg.add_attachment(
                    data, maintype="image", subtype="jpeg", filename="snapshot.jpg"
                )
            else:
                log.warning("image %s missing; sending text only", p)

        try:
            password = read_secret(self._pw_file)
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as s:
                s.starttls()
                s.login(self.user, password)
                s.send_message(msg)
        except Exception as e:
            raise SendError(f"{self.name}: {type(e).__name__}: {e}") from e


SMS_MAX_CHARS = 160


class SMSModemSink:
    """Plain text SMS through the SIM7600.

    Holds the only handle on the AT port. An AT port is single-writer: two senders
    interleaving commands both fail, and can wedge the modem until a power cycle.
    """

    type = "sms_modem"

    def __init__(self, spec: dict):
        self.name = spec.get("name", "sms")
        self.port = spec.get("port", "/dev/sim7600-at")
        self.baud = int(spec.get("baud", 115200))
        self.strict_gsm7 = bool(spec.get("strict_gsm7", True))
        self._lock = port_lock(self.port)

    def _cmd(self, ser, cmd: str, wait: float = 1.0) -> str:
        ser.reset_input_buffer()
        ser.write((cmd + "\r").encode())
        out, deadline = b"", time.time() + wait + 5
        while time.time() < deadline:
            chunk = ser.read(4096)
            if chunk:
                out += chunk
                if b"OK" in out or b"ERROR" in out:
                    break
                deadline = time.time() + 0.5
            else:
                time.sleep(0.05)
        return out.decode(errors="replace")

    def send(self, to, body: str, image_path: str | None = None) -> None:
        import serial

        if image_path:
            log.info("%s: SMS cannot carry images; sending text only", self.name)
        text = to_gsm7(body) if self.strict_gsm7 else body

        # AT+CMGS in text mode caps at 160 characters and we do not segment.
        # Truncating is better than dropping an alert, but it must be visible -
        # the warning and the marker are the record that content was lost.
        if len(text) > SMS_MAX_CHARS:
            log.warning("%s: body is %d chars, truncating to %d",
                        self.name, len(text), SMS_MAX_CHARS)
            text = text[:SMS_MAX_CHARS - 3] + "..."

        with self._lock:
            try:
                ser = serial.Serial(self.port, self.baud, timeout=1)
            except Exception as e:
                raise SendError(f"{self.name}: cannot open {self.port}: {e}") from e
            try:
                if "OK" not in self._cmd(ser, "AT"):
                    raise SendError(f"{self.name}: modem not responding")
                self._cmd(ser, "AT+CMEE=1")
                self._cmd(ser, "AT+CMGF=1")
                self._cmd(ser, "AT+CSMS=1")

                ser.reset_input_buffer()
                ser.write(f'AT+CMGS="{to.e164}"\r'.encode())
                time.sleep(1.5)
                prompt = ser.read(256)
                if b">" not in prompt:
                    raise SendError(
                        f"{self.name}: no send prompt (got {prompt!r}) - the line may "
                        f"have no MSISDN provisioned"
                    )

                ser.write(text.encode("ascii", "replace") + b"\x1A")
                out, deadline = b"", time.time() + 70
                while time.time() < deadline:
                    chunk = ser.read(4096)
                    if chunk:
                        out += chunk
                        if b"+CMGS:" in out or b"ERROR" in out:
                            break
                    else:
                        time.sleep(0.2)

                reply = out.decode(errors="replace").strip()
                if b"+CMGS:" not in out:
                    raise SendError(f"{self.name}: send rejected: {reply!r}")
            finally:
                ser.close()


class MMSNativeSink:
    """Real MMS over the cellular link - no email gateway involved.

    Builds an M-Send.req and POSTs it to the carrier MMSC through the modem's
    own TCP stack. Takes roughly 25s for a 15KB payload (12 chunked AT writes),
    and holds the modem lock throughout.
    """

    type = "mms_native"

    def __init__(self, spec: dict):
        self.name = spec.get("name", "mms")
        self.port = spec.get("port", "/dev/sim7600-at")
        self.sender = spec.get("from")
        if not self.sender:
            raise ConfigError(
                f"sink {self.name}: 'from' is required (this line's own number, "
                f"used as the MMS From address)"
            )
        img = spec.get("image") or {}
        self.max_width = int(img.get("max_width", 640))
        self.quality = int(img.get("quality", 70))
        self.subject = spec.get("subject")

    def send(self, to, body: str, image_path: str | None = None) -> None:
        from .mms_native import ModemHTTP, build_pdu, interpret, MMSError

        staged = None
        if image_path:
            p = Path(image_path)
            if p.is_file():
                # Re-encode to JPEG: the source may be PNG, and carriers are
                # much happier with a small baseline JPEG.
                try:
                    data = resize_jpeg(p.read_bytes(), self.max_width, self.quality)
                    staged = p.with_suffix(".mms.jpg")
                    staged.write_bytes(data)
                except Exception as e:
                    log.warning("%s: could not re-encode %s (%s); using original",
                                self.name, p, e)
                    staged = p
            else:
                log.warning("%s: image %s missing; sending text only", self.name, p)

        try:
            pdu = build_pdu(to=to.e164, sender=self.sender, text=body,
                            image_path=str(staged) if staged else None,
                            subject=self.subject)
        except Exception as e:
            raise SendError(f"{self.name}: could not encode PDU: {e}") from e

        try:
            with ModemHTTP(self.port) as http:
                http.connect()
                response = http.post(pdu)
        except MMSError as e:
            raise SendError(f"{self.name}: {e}") from e
        except Exception as e:
            raise SendError(f"{self.name}: {type(e).__name__}: {e}") from e

        info = interpret(response)
        if not info.get("accepted"):
            raise SendError(
                f"{self.name}: MMSC rejected the message: "
                f"{info.get('status_line')} {info.get('mmsc_status')}"
            )
        log.info("%s: MMSC accepted, message-id %s", self.name,
                 info.get("message_id"))


SINK_TYPES = {
    EmailMMSSink.type: EmailMMSSink,
    SMSModemSink.type: SMSModemSink,
    MMSNativeSink.type: MMSNativeSink,
}


def build_sinks(cfg) -> dict:
    sinks = {}
    for spec in cfg.sinks:
        name, stype = spec.get("name"), spec.get("type")
        if not name or not stype:
            raise ConfigError(f"sink missing name or type: {spec!r}")
        cls = SINK_TYPES.get(stype)
        if not cls:
            raise ConfigError(
                f"sink {name!r}: unknown type {stype!r} "
                f"(have: {', '.join(sorted(SINK_TYPES))})"
            )
        sinks[name] = cls(spec)
    return sinks
