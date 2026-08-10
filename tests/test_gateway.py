#!/usr/bin/env python3
"""Offline checks for the webhook API and the MCP protocol layer.

No modem, no SMTP, no network: sinks and HTTP calls are stubbed. Run on the Pi:
    python3 tests/test_gateway.py
"""
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, "/opt/smsgw")

from smsgw import config as cfgmod          # noqa: E402
from smsgw import mcp_server                # noqa: E402
from smsgw.http_api import Gateway, serve   # noqa: E402
from smsgw.sinks import SendError, to_gsm7  # noqa: E402
from smsgw.store import Store               # noqa: E402

FAILURES = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(label)


class StubSink:
    """Records what it was asked to send; can be told to fail."""
    type = "stub"

    def __init__(self, name, fail=False):
        self.name, self.fail, self.sent = name, fail, []

    def send(self, to, body, image_path=None):
        if self.fail:
            raise SendError(f"{self.name}: stubbed failure")
        self.sent.append((to.e164, body, image_path))


def make_gateway(tmp, routing, sinks):
    cfg = cfgmod.Config(path=Path("test"), state_dir=Path(tmp))
    if isinstance(routing, list):
        routing = {k: list(routing)
                   for k in ("with_image", "text_only", "long_text")}
    cfg.routing = routing
    cfg.limits = {"max_per_hour": 5, "max_per_day": 100}
    cfg.http_port = 18080
    store = Store(Path(tmp) / "t.db")
    return cfg, store, Gateway(cfg, store, sinks)


def main():
    tmp = tempfile.mkdtemp(prefix="smsgw-test-")

    print("\nnumber normalisation")
    r = cfgmod.Recipient("5551234567")
    check("10-digit -> E.164", r.e164, "+15551234567")
    check("email-MMS address", r.mms_address(), "5551234567@tmomail.net")
    check("E.164 passthrough", cfgmod.Recipient("+15551234567").e164, "+15551234567")

    print("\nsend + routing fallback")
    mms, sms = StubSink("mms"), StubSink("sms")
    cfg, store, gw = make_gateway(tmp, ["mms", "sms"], {"mms": mms, "sms": sms})
    res = gw.send("+15551234567", "hello")
    check("first sink used", res["via"], "mms")
    check("recorded as sent", res["status"], "sent")
    check("second sink untouched", len(sms.sent), 0)

    mms.fail = True
    res = gw.send("+15551234567", "fallback please")
    check("falls back on failure", res["via"], "sms")

    sms.fail = True
    res = gw.send("+15551234567", "nobody home")
    check("all sinks failed", res["status"], "failed")

    print("\nrate ceiling (5/hour)")
    mms.fail = sms.fail = False
    for _ in range(4):
        gw.send("+15551234567", "spam")
    res = gw.send("+15551234567", "one too many")
    check("ceiling refuses send", res["status"], "failed")
    check("ceiling reason surfaced", "hourly ceiling" in res["error"], True)

    print("\ninbound + dedupe")
    cfg2, store2, gw2 = make_gateway(tmp + "2", ["mms"], {"mms": StubSink("mms")})
    Path(tmp + "2").mkdir(exist_ok=True)
    cfg2, store2, gw2 = make_gateway(tmp, ["mms"], {"mms": StubSink("mms")})
    before = store2.counts()["inbound"]
    gw2.on_inbound("+15551234567", "hi there", "26/08/09,14:00:00")
    gw2.on_inbound("+15551234567", "hi there", "26/08/09,14:00:00")
    check("duplicate inbound ignored", store2.counts()["inbound"] - before, 1)

    print("\nroute selection")
    mms_s, sms_s = StubSink("mms_native"), StubSink("sms")
    cfg3, store3, gw3 = make_gateway(
        tmp,
        {"with_image": ["mms_native", "sms"], "long_text": ["mms_native", "sms"],
         "text_only": ["sms"]},
        {"mms_native": mms_s, "sms": sms_s})
    check("short text -> text_only",
          gw3.choose_route(False, "hi", None)[0], "text_only")
    check("with image -> with_image",
          gw3.choose_route(True, "hi", None)[0], "with_image")
    check("long text promoted to MMS",
          gw3.choose_route(False, "x" * 200, None)[0], "long_text")
    check("161 chars is long",
          gw3.choose_route(False, "x" * 161, None)[0], "long_text")
    check("160 chars is not",
          gw3.choose_route(False, "x" * 160, None)[0], "text_only")
    check("force=sms overrides an image",
          gw3.choose_route(True, "hi", "sms")[0], "forced-sms")
    check("force=mms overrides short text",
          gw3.choose_route(False, "hi", "mms")[0], "forced-mms")
    check("forced sms uses the text chain",
          gw3.choose_route(True, "hi", "sms")[1], ["sms"])

    print("\nHTTP API")
    serve(cfg, gw)
    base = f"http://127.0.0.1:{cfg.http_port}"

    def post(path, payload):
        req = urllib.request.Request(
            base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(path):
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    code, body = post("/send", {"text": "no recipient"})
    check("POST /send rejects missing 'to'", code, 400)

    code, body = post("/send", {"to": "+15551234567", "text": "x", "force": "carrier-pigeon"})
    check("POST /send rejects a bad force value", code, 400)

    code, body = post("/send", {"to": "+15551234567", "text": "over ceiling"})
    check("POST /send reports sink failure as 502", code, 502)

    code, body = get("/messages?limit=3")
    check("GET /messages returns list", isinstance(body["messages"], list), True)

    first_id = body["messages"][0]["id"]
    code, body = get(f"/messages?since={first_id}&limit=2")
    check("since= excludes the cursor row",
          all(m["id"] > first_id for m in body["messages"]), True)

    code, body = get("/messages/999999")
    check("unknown message id -> 404", code, 404)

    print("\nMCP protocol")
    resp = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2024-11-05",
                                         "clientInfo": {"name": "test"}}})
    check("initialize echoes known version",
          resp["result"]["protocolVersion"], "2024-11-05")
    resp = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                              "params": {"protocolVersion": "1999-01-01"}})
    check("unknown version falls back to newest",
          resp["result"]["protocolVersion"], mcp_server.SUPPORTED[0])
    check("advertises tools capability",
          "tools" in resp["result"]["capabilities"], True)

    resp = mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    check("notification gets no reply", resp, None)

    resp = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    names = [t["name"] for t in resp["result"]["tools"]]
    check("tools/list names", sorted(names),
          ["gateway_health", "list_messages", "send_message"])
    check("every tool has a schema",
          all("inputSchema" in t for t in resp["result"]["tools"]), True)

    resp = mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "nope/nope"})
    check("unknown method -> -32601", resp["error"]["code"], -32601)

    # Point the MCP client at the live test server and drive a real tool call.
    mcp_server.BASE_URL = base
    resp = mcp_server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                              "params": {"name": "list_messages",
                                         "arguments": {"limit": 2}}})
    check("tools/call returns content blocks",
          resp["result"]["content"][0]["type"], "text")
    check("successful call is not an error", resp["result"]["isError"], False)

    resp = mcp_server.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                              "params": {"name": "send_message", "arguments": {}}})
    check("missing argument -> isError, not RPC error",
          resp["result"]["isError"], True)
    check("missing argument still returns a result", "error" in resp, False)

    print("\nGSM-7 transliteration")
    check("emoji stripped", to_gsm7("Person 🚨 seen"), "Person  seen")
    check("smart quotes folded", to_gsm7("it’s here"), "it's here")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
