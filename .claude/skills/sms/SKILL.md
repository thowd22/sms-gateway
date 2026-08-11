---
name: sms
description: Send and receive SMS/MMS through the smsgw cellular gateway via its MCP tools (send_message, list_messages, gateway_health). Use whenever the user asks to text or message a phone number or a person by name, send a picture to a phone, check for replies or incoming texts, wait for someone's response, or diagnose the SMS gateway/modem. Also covers the address book of named recipients, cost rules (SMS vs MMS), polling for replies, and what to do when a send fails.
---

# Texting through the smsgw gateway

The `smsgw` MCP server fronts a Raspberry Pi with a cellular modem that sends real
SMS and MMS over the mobile network. Three tools: `send_message`, `list_messages`,
`gateway_health`.

## The one rule that matters

**Every message costs real money and lands on someone's actual phone.** There is no
sandbox, no dry run, and no unsend. Send only what the user asked you to send, to the
number they gave. Never send a test message to check whether the gateway works — use
`gateway_health` for that, it is free.

Before a first send in a conversation, make sure you have the number right. If the
user says "text Mom" and you have no number on file, ask; do not guess from context or
from a number that appears in unrelated files.

## Address Book

So the user can say "text Sarah" instead of reciting ten digits.

**No real numbers in this file.** This repo is public. What follows is the template;
the live address book lives in the user's local copy of this skill at
`~/.claude/skills/sms/SKILL.md`, which is never committed. Keep the two in sync for
everything *except* the table.

| name | number | notes |
|---|---|---|
| _Example_ | `+15551234567` | who they are, what they should be texted about |

Resolve a name **only** from a row in the local copy. If the name isn't there, ask for
the number. Do not infer one from `list_messages` history, from other files on the
machine, or from a number that happens to appear earlier in the conversation — a
number that has traffic is not the same as a number the user meant.

When two rows could match a name the user gave ("Mike"), ask which; don't pick the
first or the more recently used.

Read the resolved number back when you report the send: "Sent SMS #52 to Sarah
(+1 555-123-4567)." The user needs to see which number you chose, because that is the
step they cannot otherwise check.

Adding, changing, or removing a row is the user's call. Don't add someone because
their number showed up in the message history, and don't quietly correct a row that
looks wrong — say what looks wrong and let them decide.

One reserved row worth keeping in the local copy: the gateway's **own** number. It is
a sink for carrier notices, not a person, and texting it is always a mistake.

## Sending

```
send_message(to="+15551234567", text="Package arrived.")
```

- `to` — E.164 preferred (`+1` prefix). A bare 10-digit US number usually works, but
  prefer the `+` form.
- `text` — the body. Keep it short; the user's recipient reads it on a phone.
- `image_url` — optional. The gateway fetches the image, re-encodes it, and sends a
  real MMS. Costs more than a text.
- `force` — `"sms"` or `"mms"`. Omit it. The gateway routes by content and gets it
  right; overriding is for when the user explicitly asks to avoid MMS cost or to force
  a picture message.

### Transport is chosen for you

| what you send | goes out as |
|---|---|
| text ≤ 160 chars | SMS (cheapest) |
| text > 160 chars | MMS, falling back to SMS |
| anything with `image_url` | MMS, falling back to text-only SMS |

Because 160 characters is the SMS cap, a long message silently becomes an MMS and
costs more. If the user is cost-sensitive, tighten the wording rather than passing
`force="sms"` — forcing SMS on a >160-char body risks truncation.

A picture that can't be fetched is dropped and the text still goes out. The result
line reports `via` (`sms_modem`, `mms_native`, `email_mms`) and `route`, so read it
back to the user when it differs from what they expected — "sent as SMS, the image
URL couldn't be fetched" is information they need.

### Reporting a send

Say what actually happened, including the message id: "Sent SMS #47 to +15551234567."
Don't claim delivery — the gateway confirms handoff to the carrier, not that the
recipient's phone rang.

## Reading messages and waiting for replies

`list_messages` returns oldest-first, one line per message, `<-` for received and
`->` for sent:

```
#46 -> +15551234567: Package arrived.
#47 <- +15551234567: thanks!
```

To poll for a reply, capture the id of your outbound message and pass it as `since`:

```
list_messages(since=46)      # only what arrived after #46
```

That is the whole polling idiom — `since` avoids re-reading history and avoids
mistaking an old message for a new reply. Never conclude "they replied" from a
message whose id is older than your send.

Other arguments: `limit` (default 50, cap 500) and `direction` (`"in"` or `"out"`).

When the user asks you to wait for a reply, poll on a human timescale — a check every
minute or two, not a tight loop. People take minutes to answer texts. Tell the user
you're waiting and stop after a reasonable interval rather than polling indefinitely;
if nothing arrives, say so plainly instead of inventing a plausible response.

## When a send fails

Call `gateway_health` first. It is free and it distinguishes the failure modes:

- `modem` — registration state, signal strength, carrier. Not registered or very weak
  signal means the problem is the radio, not your request. Nothing to retry.
- `sinks` — which transports are configured. No `mms_native` means pictures can only
  go out via the email fallback, which needs the site's internet connection.
- `counts` — totals including `failed`.

If the gateway is unreachable at all, the tool says so — that is the LAN/host being
down, not the modem. Report which layer failed rather than retrying blindly.

Do not retry a failed send more than once, and never retry in a loop: each attempt
that partially succeeded may have already cost money and may have already reached the
recipient. Prefer telling the user what failed and asking how to proceed.

## Rate limits

The gateway enforces its own hourly and daily send caps (defaults 30/hour, 150/day).
Hitting one is a configuration boundary, not a bug — report it and stop. Never work
around a rate limit by splitting a message across sends.
