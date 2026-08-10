# smsgw — a small SMS/MMS gateway

Runs on `sms-host` (192.0.2.62) with the SIM7600G-H HAT. Two interfaces over one
transport: **HTTP webhooks** for machines, and an **MCP server** for agents.

Goal is to avoid paid services like Twilio. Two things make the SIM worth having
beyond price: no A2P 10DLC campaign registration to satisfy, and a number nobody
else's compliance team can switch off.


## Shape

The gateway is a **transport**, not a rules engine. It sends what it is told to
send and reports what it receives. Whatever decides *when* to send lives outside.

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

Three decisions worth stating:

**One process owns the modem.** An AT port is single-writer. Sending, inbound
polling, and the `/health` probe all take the same lock — a health check that
lands between `AT+CMGS` and its `>` prompt corrupts both operations. This was a
real bug in the first cut, caught by probing `/health` while the poller ran.

**Inbound is polled, not interrupt-driven.** A `+CMTI` unsolicited code that
arrives while the port is mid-send is simply lost, and a missed inbound message
is invisible. One `AT+CMGL` every 20s cannot miss.

**Delivery to webhooks is best effort, but nothing is lost.** A dead subscriber
doesn't drop the message: it's already in the store, and `GET /messages?since=`
will still return it. That cursor is what makes the API pollable.


## How pictures get out

**SMS cannot carry an image** — 160 characters of GSM-7 and that is the protocol.
Pictures require MMS, and this modem has no MMS client. Confirmed twice:

```
AT+CLAC -> 643 commands supported, MMS commands: NONE
SIM7500/SIM7600 AT manual -> zero occurrences of CMMS
```

Its only four "MMS" hits are SIM elementary-file names (`EF_MMSN` and friends —
the SIM's MMS *provisioning* storage). MMS is a **SIM800**-family feature that
spec sheets copy across families. Flashing does not help: we are already on the
newest `LE20B04SIM7600G22`. The HAT's SD slot is general file storage, not MMS
staging.

**So we build the MMS ourselves.** `mms_native` encodes an `M-Send.req` PDU and
POSTs it to T-Mobile's MMSC over the cellular link — no email gateway, no
dependency on this building's internet. Verified working: the MMSC returns
`1000:OK` with a UUID Message-ID, and the picture arrives.

| sink | how | picture | needs |
|---|---|---|---|
| `mms_native` | hand-built `M-Send.req` POSTed to the MMSC via the modem's TCP stack | yes | activated SIM, data |
| `sms_modem` | `AT+CMGS` over the HAT | no | activated SIM |
| `email_mms` | SMTP to `<number>@tmomail.net` | yes | this building's internet, SMTP creds |

`email_mms` stays configured but out of the routing chains — it exists as a
fallback if the cellular path ever fails, but depending on the internet defeats
the point.

### Building the PDU

`smsgw/vendor/messaging` is pmarti's `python-messaging`, mechanically ported from
Python 2. It is vendored rather than reimplemented because its **WSP well-known
value tables** are the part that must be exactly right — a wrong content-type
byte produces a PDU the MMSC rejects with no useful diagnostic. The port was ~20
mechanical fixes plus two real ones: image files were opened in **text** mode,
and `ord()` was called on bytes that iterate as ints on py3.

Every PDU is checked by encoding then decoding it with the library's own decoder
before it goes near the carrier.

### Why the modem's TCP stack, not the Pi's

The MMSC lives at a carrier-internal `10.177.171.x` address, reachable only from
inside the mobile network. Routing that from the Pi means bringing up `wwan0` —
and this modem's QMI **WDS service refuses to allocate a client** (DMS works,
WDS times out). The modem's own stack is already bound to the PDP context, so it
sidesteps routing entirely. Payload goes out in 1400-byte chunks because
`AT+CIPSEND` caps at 1500.

**IPv4 is not a problem**, contrary to a first guess: `AT+CGDCONT=<cid>,"IP",...`
grants a v4 address on request. The default context is `IPV4V6` and often gets
only v6, which is not the same thing as v4 being unavailable.


## HTTP API

No auth, no TLS, LAN only. **Anything on the LAN that reaches this port can send
messages on your account and read every message received.** That is the tradeoff;
keep the port off any interface you don't control.

```
POST /send
  {"to": "+15551234567", "text": "Person on driveway",
   "image_url": "http://...",     // optional -> MMS
   "force": "sms" | "mms"}        // optional transport override
  -> 200 {"id": 12, "status": "sent", "via": "mms_native", "route": "with_image"}
  -> 502 {"id": 13, "status": "failed", "error": "..."}

GET /messages?since=<id>&limit=<n>&direction=in|out
GET /messages/<id>
GET /health          modem registration, signal, carrier, message counts
```

### One endpoint, not send_sms + send_mms

Content picks the transport; the caller does not have to know carrier mechanics.
Two endpoints would create states that cannot be right — `send_sms` with an
image (drop it, or error?) and `send_mms` without one (an SMS that costs more) —
and every caller would re-derive the same rule, one of them wrongly.

Fallback is the other reason. Native MMS depends on the data path, which can
fail while SMS is still fine; when it does, the text should still go. That is a
server concern, and the response reports `via` and `route` so the caller still
learns what actually happened.

`force` exists for the two cases where the caller genuinely knows better:
skipping the cost of an MMS, or insisting on one.

| content | chain | why |
|---|---|---|
| has image | `mms_native -> sms` | picture if possible, text if not |
| text > 160 chars | `mms_native -> sms` | `AT+CMGS` caps at 160 and we do not segment |
| short text | `sms` | cheapest thing that works |

The long-text rule matters: without it a long alert is silently truncated. The
SMS sink still truncates as a last resort, but it logs a warning and marks the
message rather than losing content quietly.

`image_base64` works in place of `image_url` when the caller already has the bytes.
A picture that can't be fetched is logged and the message goes out as text —
losing the image must not lose the alert.

Inbound messages are POSTed as JSON to every URL in `forward:`.


## MCP server

`smsgw/mcp_server.py` — stdio transport, JSON-RPC 2.0, no dependencies. It is a
thin client over the HTTP API and holds no state, so it runs wherever the MCP host
runs and reaches the gateway over the LAN.

Tools: `send_message`, `list_messages`, `gateway_health` — one send tool, not
two, for the same reasons as the webhook. Two tools would double the chance the
model picks wrong, and the distinction it would choose on is already visible in
the arguments.

```jsonc
// Claude Code MCP config
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

- **Transport is newline-delimited JSON on stdin/stdout**, one message per line.
  Nothing may be written to stdout except protocol messages — a stray `print()`
  corrupts the stream. All logging goes to stderr.
- **Version negotiation:** `initialize` echoes the client's `protocolVersion` when
  we know it, else our newest (`2025-06-18`). Supported: `2025-06-18`,
  `2025-03-26`, `2024-11-05`.
- **Notifications carry no `id` and must never get a response** —
  `notifications/initialized` is the one you'll see immediately after init.
- **A failed tool is a successful RPC** carrying `isError: true`, not a JSON-RPC
  error. That's what lets the model see the failure and react to it, rather than
  the host swallowing it as a transport fault.


## What this doesn't do any more

The earlier design had an MQTT source and a YAML rule engine (camera allowlists,
per-camera throttles, quiet hours, templating). That moved out — it's in `legacy/`
for reference, not on the running path.

**Consequence worth naming: nothing currently turns a Frigate alert into a send.**
Frigate publishes MQTT; it cannot call a webhook. Closing that needs one of:

- a small MQTT→webhook bridge (subscribe `frigate/reviews`, POST `/send`) — about
  40 lines, and the natural home for camera filtering and cooldowns;
- Frigate's own native push notifications, if the pictures only need to reach a
  phone that has internet;
- an agent driving `send_sms` over MCP, for ad-hoc rather than automatic alerts.


## Operating

```
systemctl status smsgw
smsgw check            validate config
smsgw status           modem state + counts
smsgw send <num> <text> [--image PATH]
python3 tests/test_gateway.py
```

Runs as the `smsgw` system user. Primary group is `dialout` for the modem;
`SupplementaryGroups=smsgw` grants read on `/etc/smsgw` — a primary group does not
imply the user's other groups, which is what broke the first start.

`/etc/smsgw/smtp.pass` must be mode 600 and owned by `smsgw`; the loader refuses
to read a secret that group or world can see.
