#!/usr/bin/env python3
"""MCP server for smsgw — stdio transport, JSON-RPC 2.0, no dependencies.

This is a thin client over the HTTP webhook API, so it runs wherever the MCP host
runs (your laptop) and talks to the gateway on the Pi over the LAN. It holds no
state of its own.

  SMSGW_URL=http://192.0.2.62:8080 python3 -m smsgw.mcp_server

Transport is newline-delimited JSON on stdin/stdout: one JSON-RPC message per
line. Nothing may be written to stdout except protocol messages - a stray print()
corrupts the stream, which is why logging goes to stderr.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("SMSGW_URL", "http://127.0.0.1:8080").rstrip("/")
TIMEOUT = int(os.environ.get("SMSGW_TIMEOUT", "30"))

# Versions this server understands. Newest first: on initialize we echo back the
# client's version if we know it, else our newest, and the client decides whether
# it can live with that.
SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS = [
    {
        "name": "send_message",
        "description": (
            "Send a message to a phone number through the household gateway. "
            "Include image_url to attach a picture; the gateway then sends it as "
            "a real MMS over the cellular network, which costs more than a "
            "text-only message. Long text is promoted to MMS automatically "
            "because SMS caps at 160 characters. Every message costs money - do "
            "not send test messages unless asked."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Destination phone number, E.164 preferred "
                                   "(e.g. +15551234567).",
                },
                "text": {"type": "string", "description": "Message body."},
                "image_url": {
                    "type": "string",
                    "description": "Optional URL of an image to attach. The "
                                   "gateway fetches, re-encodes and sends it as MMS.",
                },
                "force": {
                    "type": "string",
                    "enum": ["sms", "mms"],
                    "description": "Override the transport. Use 'sms' to avoid "
                                   "the cost of an MMS, 'mms' to force one. Omit "
                                   "this unless you have a specific reason.",
                },
            },
            "required": ["to", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_messages",
        "description": (
            "List messages the gateway has sent or received, oldest first. Pass "
            "since (a message id) to get only what is newer - this is how you poll "
            "for replies without re-reading history."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "integer",
                          "description": "Return messages with id greater than this."},
                "limit": {"type": "integer",
                          "description": "Maximum to return (default 50, cap 500)."},
                "direction": {"type": "string", "enum": ["in", "out"],
                              "description": "Restrict to received or sent."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "gateway_health",
        "description": (
            "Check the gateway: modem registration, signal strength, carrier, and "
            "message counts. Use this first when a send fails."
        ),
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
    },
]


def log(msg: str) -> None:
    print(f"[smsgw-mcp] {msg}", file=sys.stderr, flush=True)


def http(method: str, path: str, payload=None):
    url = BASE_URL + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return json.loads(body)
        except ValueError:
            return {"error": f"HTTP {e.code}: {body[:200]}"}
    except Exception as e:
        return {"error": f"cannot reach gateway at {BASE_URL}: {e}"}


# --- tool implementations -----------------------------------------------------

def call_tool(name: str, args: dict) -> tuple[str, bool]:
    """Return (text, is_error)."""
    if name == "send_message":
        body = {"to": args["to"], "text": args["text"]}
        for key in ("image_url", "force"):
            if args.get(key):
                body[key] = args[key]
        result = http("POST", "/send", body)
        if result.get("status") == "sent":
            kind = "MMS" if result.get("via", "").startswith("mms") else "SMS"
            return (f"Sent {kind} #{result['id']} to {args['to']} "
                    f"(via {result.get('via')}, route {result.get('route')})."), False
        return f"Send failed: {result.get('error', result)}", True

    if name == "list_messages":
        params = {}
        for key in ("since", "limit", "direction"):
            if args.get(key) is not None:
                params[key] = args[key]
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        result = http("GET", "/messages" + query)
        if "error" in result:
            return f"Could not list messages: {result['error']}", True
        msgs = result.get("messages", [])
        if not msgs:
            return "No messages.", False
        lines = []
        for m in msgs:
            arrow = "<-" if m["direction"] == "in" else "->"
            flag = "" if m["status"] == "ok" else f" [{m['status']}: {m.get('error')}]"
            lines.append(f"#{m['id']} {arrow} {m['peer']}: {m['text']}{flag}")
        return "\n".join(lines), False

    if name == "gateway_health":
        result = http("GET", "/health")
        if "error" in result:
            return f"Gateway unreachable: {result['error']}", True
        return json.dumps(result, indent=2), False

    return f"Unknown tool: {name}", True


# --- JSON-RPC plumbing --------------------------------------------------------

def handle(msg: dict):
    """Return a response dict, or None for notifications (which get no reply)."""
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": code, "message": message}}

    if method == "initialize":
        wanted = params.get("protocolVersion")
        version = wanted if wanted in SUPPORTED else SUPPORTED[0]
        client = (params.get("clientInfo") or {}).get("name", "unknown")
        log(f"initialize from {client} (wants {wanted}, using {version})")
        return ok({
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "smsgw", "version": "0.2.0"},
        })

    # Notifications carry no id and must never get a response.
    if msg_id is None:
        log(f"notification: {method}")
        return None

    if method == "tools/list":
        return ok({"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            text, is_error = call_tool(name, args)
        except KeyError as e:
            text, is_error = f"Missing required argument: {e}", True
        except Exception as e:
            log(f"tool {name} raised: {type(e).__name__}: {e}")
            text, is_error = f"{type(e).__name__}: {e}", True
        # A failed tool is a successful RPC carrying isError, not a JSON-RPC
        # error - that is what lets the model see the failure and react.
        return ok({"content": [{"type": "text", "text": text}], "isError": is_error})

    if method == "ping":
        return ok({})

    return err(-32601, f"Method not found: {method}")


def main() -> int:
    log(f"started, gateway = {BASE_URL}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError as e:
            log(f"bad JSON on stdin: {e}")
            continue
        try:
            response = handle(msg)
        except Exception as e:
            log(f"handler crashed: {type(e).__name__}: {e}")
            response = {"jsonrpc": "2.0", "id": msg.get("id"),
                        "error": {"code": -32603, "message": str(e)}}
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    log("stdin closed, exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
