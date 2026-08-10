"""HTTP webhook API.

Plain stdlib http.server - no framework, no auth, no TLS, LAN only.

  POST /send        {"to": "+1...", "text": "...", "image_url"|"image_base64": ...}
  GET  /messages    ?since=<id>&limit=<n>&direction=in|out
  GET  /messages/<id>
  GET  /health

Anything on the LAN that can reach this port can send messages on your account and
read every message received. That is the tradeoff of no auth; keep the port off any
interface you do not control.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .modem import modem_status
from .sinks import SMS_MAX_CHARS, SendError, to_gsm7
from .store import row_to_dict

log = logging.getLogger(__name__)

MAX_BODY = 8 * 1024 * 1024   # generous enough for a base64 JPEG, bounded enough
                             # that a stray upload can't exhaust memory


class Gateway:
    """Owns sending. The HTTP handler and the MCP server both call through here."""

    def __init__(self, cfg, store, sinks):
        self.cfg = cfg
        self.store = store
        self.sinks = sinks
        # The modem sink owns the AT port's lock; /health must borrow it rather
        # than opening the port behind the send path's back.
        self.modem_sink = next(
            (s for s in sinks.values() if getattr(s, "type", "") == "sms_modem"), None
        )
        self.image_dir = Path(cfg.state_dir) / "images"
        self.image_dir.mkdir(parents=True, exist_ok=True)

    def choose_route(self, has_image: bool, text: str,
                     force: str | None = None) -> tuple[str, list[str]]:
        """Pick a sink chain from the content, unless the caller overrides it.

        Content decides by default because the caller should not have to know
        carrier mechanics - but 'force' exists for the two cases where they do
        know better: skipping the cost of an MMS, or insisting on one.
        """
        if force == "sms":
            return "forced-sms", self.cfg.routing["text_only"]
        if force == "mms":
            return "forced-mms", self.cfg.routing["with_image"]
        if has_image:
            return "with_image", self.cfg.routing["with_image"]
        # SMS cannot segment here, so long text goes as MMS rather than being
        # truncated. MMS carries it natively and often costs less than the
        # equivalent SMS segments would.
        if len(to_gsm7(text)) > SMS_MAX_CHARS:
            return "long_text", self.cfg.routing["long_text"]
        return "text_only", self.cfg.routing["text_only"]

    def send(self, to: str, text: str, image_path: str | None = None,
             carrier: str | None = None, meta: dict | None = None,
             force: str | None = None) -> dict:
        reason = self.store.rate_exceeded(
            int(self.cfg.limits.get("max_per_hour", 0)),
            int(self.cfg.limits.get("max_per_day", 0)),
        )
        if reason:
            log.error("RATE CEILING: refusing send to %s - %s", to, reason)
            msg_id = self.store.record("out", to, text, image_path,
                                       status="failed", error=reason, meta=meta)
            return {"id": msg_id, "status": "failed", "error": reason}

        recipient = self.cfg.recipient(to, carrier)
        reason, chain = self.choose_route(bool(image_path), text, force)
        log.info("routing %s via %s (%s)", recipient.e164, " -> ".join(chain), reason)

        errors = []
        for name in chain:
            sink = self.sinks.get(name)
            if not sink:
                continue
            # A sink that can't carry an image is still worth trying for the text,
            # but only after every image-capable sink has failed.
            try:
                sink.send(recipient, text, image_path)
                msg_id = self.store.record("out", recipient.e164, text, image_path,
                                           status="ok", via=name,
                                           meta={**(meta or {}), "route": reason})
                log.info("#%d sent to %s via %s", msg_id, recipient.e164, name)
                return {"id": msg_id, "status": "sent", "via": name, "route": reason}
            except SendError as e:
                log.warning("sink %s failed: %s", name, e)
                errors.append(str(e))
            except Exception as e:
                log.exception("sink %s raised", name)
                errors.append(f"{name}: {e}")

        error = "; ".join(errors) or "all sinks failed"
        msg_id = self.store.record("out", recipient.e164, text, image_path,
                                   status="failed", error=error, meta=meta)
        return {"id": msg_id, "status": "failed", "error": error}

    def save_image(self, *, url: str | None = None, b64: str | None = None) -> str:
        path = self.image_dir / f"{int(time.time() * 1000)}.jpg"
        if url:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = r.read(MAX_BODY)
        elif b64:
            data = base64.b64decode(b64)
        else:
            raise ValueError("no image source")
        path.write_bytes(data)
        return str(path)

    # --- inbound --------------------------------------------------------------

    def on_inbound(self, sender: str, text: str, stamp: str) -> None:
        if self.store.seen_inbound(sender, text):
            log.debug("inbound from %s already recorded, skipping", sender)
            return
        msg_id = self.store.record("in", sender, text, meta={"modem_ts": stamp})
        log.info("#%d received from %s: %s", msg_id, sender, text[:60])
        self.forward(row_to_dict(self.store.get(msg_id)))

    def forward(self, payload: dict) -> None:
        """POST an inbound message to each configured webhook, best effort."""
        for url in self.cfg.forward:
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    log.info("forwarded #%s to %s (%s)", payload.get("id"), url,
                             r.status)
            except Exception as e:
                # A dead subscriber must not lose the message - it stays in the
                # store and /messages?since= will still return it.
                log.warning("forward to %s failed: %s", url, e)


class Handler(BaseHTTPRequestHandler):
    gateway: Gateway = None  # set by serve()
    server_version = "smsgw"

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        gw = self.gateway

        if url.path == "/health":
            status = modem_status(
                gw.cfg.modem_port,
                lock=gw.modem_sink._lock if gw.modem_sink else None,
            )
            return self._json(200, {
                "ok": True,
                "modem": status,
                "sinks": list(gw.sinks),
                "routing": gw.cfg.routing,
                "counts": gw.store.counts(),
            })

        if url.path == "/messages":
            rows = gw.store.since(
                since_id=int(q.get("since", [0])[0]),
                limit=int(q.get("limit", [50])[0]),
                direction=(q.get("direction") or [None])[0],
            )
            return self._json(200, {"messages": [row_to_dict(r) for r in rows]})

        if url.path.startswith("/messages/"):
            row = gw.store.get(int(url.path.rsplit("/", 1)[1]))
            if not row:
                return self._json(404, {"error": "not found"})
            return self._json(200, row_to_dict(row))

        return self._json(404, {"error": "not found"})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path != "/send":
            return self._json(404, {"error": "not found"})

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "body too large"})
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError as e:
            return self._json(400, {"error": f"invalid JSON: {e}"})

        to, text = payload.get("to"), payload.get("text")
        if not to or not text:
            return self._json(400, {"error": "'to' and 'text' are required"})

        image_path = None
        if payload.get("image_url") or payload.get("image_base64"):
            try:
                image_path = self.gateway.save_image(
                    url=payload.get("image_url"), b64=payload.get("image_base64")
                )
            except Exception as e:
                # Losing the picture must not lose the alert.
                log.warning("could not fetch image: %s; sending text only", e)

        force = payload.get("force")
        if force not in (None, "sms", "mms"):
            return self._json(400, {"error": "'force' must be 'sms' or 'mms'"})

        result = self.gateway.send(
            to=to, text=text, image_path=image_path,
            carrier=payload.get("carrier"), meta=payload.get("meta"), force=force,
        )
        return self._json(200 if result["status"] == "sent" else 502, result)


def serve(cfg, gateway) -> ThreadingHTTPServer:
    Handler.gateway = gateway
    httpd = ThreadingHTTPServer((cfg.http_host, cfg.http_port), Handler)
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    log.info("http listening on %s:%d", cfg.http_host, cfg.http_port)
    return httpd
