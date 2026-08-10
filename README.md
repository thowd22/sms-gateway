# sms-gateway (smsgw)

Send and receive SMS and **real MMS** from a Raspberry Pi with a SIM7600 4G HAT,
over the cellular network — no Twilio, no email-to-MMS gateway, no dependency on
the site's internet connection.

Two interfaces over one transport: **HTTP webhooks** for machines, and an **MCP
server** for agents.

```
   POST /send ──────┐        route by content        ┌── mms_native (text+picture,
                    ├──▶  gateway  ──▶ chain  ──▶    │      over the cellular link)
   MCP send_message ┘        │                       ├── sms_modem  (text only)
                             │                       └── email_mms  (fallback,
                        message log                        needs the internet)
                        (SQLite, in + out)
                             │
   inbound SMS ──────────────┼──▶ POST to each forward URL
   (modem poll)              └──▶ GET /messages?since=<id>
```

Companion project: [mqtt-sms-bridge](https://github.com/thowd22/mqtt-sms-bridge)
turns MQTT messages into calls to this gateway.

## The interesting part: MMS on a modem that has no MMS

The SIM7600 has **no MMS client**. Confirmed two ways:

```
AT+CLAC                    -> 643 commands supported, MMS commands: NONE
SIM7500/SIM7600 AT manual  -> zero occurrences of CMMS
```

MMS is a SIM800-family feature; spec sheets copy the bullet across families.
Newer firmware does not add it — this was tested on the newest
`LE20B04SIM7600G22`.

So `smsgw/mms_native.py` builds the `M-Send.req` PDU itself and POSTs it to the
carrier's MMSC through the modem's own TCP stack. That works because the MMSC
lives on a carrier-internal address reachable only from inside the mobile
network, and the modem's stack is already bound to the PDP context — which
sidesteps having to route it from the host. (This modem's QMI WDS service
refuses to allocate a client anyway: DMS works, WDS times out.)

Payload goes out in 1400-byte chunks because `AT+CIPSEND` caps at 1500.

Two things that are easy to get wrong:

- **IPv4 is available on request.** `AT+CGDCONT=<cid>,"IP",<apn>` gets a v4
  address. A default `IPV4V6` context is often granted v6 only, which is not the
  same thing as v4 being unavailable.
- **The AT port is single-writer.** Sending SMS, sending MMS, polling for
  inbound, and the health probe all take one shared lock. A health check landing
  between `AT+CMGS` and its `>` prompt corrupts both operations.

## HTTP API

No auth, no TLS — LAN only by design. **Anything that reaches this port can send
messages on your account and read every message received.** Keep it off any
interface you do not control.

```
POST /send
  {"to": "+15551234567", "text": "...",
   "image_url": "http://...",     // optional -> MMS
   "force": "sms" | "mms"}        // optional transport override
  -> {"id": 12, "status": "sent", "via": "mms_native", "route": "with_image"}

GET  /messages?since=<id>&limit=<n>&direction=in|out
GET  /messages/<id>
GET  /health
```

`image_base64` works in place of `image_url`. A picture that cannot be fetched
is logged and the message goes out as text — losing the image must not lose the
alert.

### One endpoint, not send_sms + send_mms

Content picks the transport. Two endpoints would create states that cannot be
right — `send_sms` with an image (drop it, or error?) and `send_mms` without one
(an SMS that costs more) — and every caller would re-derive the same rule.

| content | chain | why |
|---|---|---|
| has image | `mms_native -> sms` | picture if possible, text if not |
| text > 160 chars | `mms_native -> sms` | `AT+CMGS` caps at 160 and this does not segment |
| short text | `sms` | cheapest thing that works |

Fallback is the other reason: MMS needs the data path, which can fail while SMS
is fine. The response reports `via` and `route`, so the caller still learns what
actually happened.

## MCP server

`smsgw/mcp_server.py` — stdio, JSON-RPC 2.0, no dependencies. A thin client over
the HTTP API holding no state, so it runs wherever the MCP host runs.

Tools: `send_message`, `list_messages`, `gateway_health`.

```jsonc
{
  "mcpServers": {
    "smsgw": {
      "command": "python3",
      "args": ["-m", "smsgw.mcp_server"],
      "env": { "PYTHONPATH": "/opt/smsgw", "SMSGW_URL": "http://192.0.2.62:8080" }
    }
  }
}
```

Protocol notes worth keeping:

- Transport is newline-delimited JSON on stdin/stdout. **Nothing may be written
  to stdout except protocol messages** — a stray `print()` corrupts the stream,
  which is why logging goes to stderr.
- `initialize` echoes the client's `protocolVersion` when known, else the newest
  supported (`2025-06-18`, `2025-03-26`, `2024-11-05`).
- Notifications carry no `id` and must never get a response.
- **A failed tool is a successful RPC** carrying `isError: true`, not a JSON-RPC
  error — that is what lets the model see the failure and react.

## Install

Requires Python 3.11+, `pyserial`, `PyYAML`, `Jinja2`, `Pillow`.

```sh
sudo mkdir -p /opt/smsgw /etc/smsgw /var/lib/smsgw
sudo cp -r smsgw vendor /opt/smsgw/
sudo cp etc/smsgw.yaml.example /etc/smsgw/smsgw.yaml   # then edit it
sudo cp etc/smsgw.service /etc/systemd/system/
sudo useradd --system --no-create-home --shell /usr/sbin/nologin --groups dialout smsgw
sudo chown -R smsgw:smsgw /var/lib/smsgw
sudo chown root:smsgw /etc/smsgw /etc/smsgw/smsgw.yaml
sudo chmod 750 /etc/smsgw && sudo chmod 640 /etc/smsgw/smsgw.yaml
sudo systemctl enable --now smsgw
```

Two deployment traps worth stating:

- **`SupplementaryGroups=smsgw` is required in the unit.** The primary group is
  `dialout` so the process can open the modem, and a primary group does not
  imply the user's other groups — without it, the daemon cannot read its own
  config.
- **Free the serial port.** If the modem is on the GPIO UART, remove
  `console=serial0,115200` from `cmdline.txt` and mask `serial-getty@ttyS0`, or
  the kernel logs into the modem and a login prompt holds the port.

Commands: `smsgw run | check | status | send <number> <text> [--image PATH]`.
Tests: `python3 tests/test_gateway.py` (no modem or SMTP required).

## Licence

**GPL-2.0**, because `vendor/messaging` (the MMS PDU encoder) is GPL-2 and this
project links it. See `vendor/README.md` for what was modified and why.
