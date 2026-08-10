"""smsgw daemon and CLI.

  smsgw run                       serve the webhook API and poll for inbound SMS
  smsgw check                     validate config and exit
  smsgw send <number> <text>      send one message now (--image PATH)
  smsgw status                    modem state and message counts
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading

from . import config as cfgmod
from .http_api import Gateway, serve
from .modem import ModemInbox, modem_status
from .sinks import build_sinks
from .store import Store, row_to_dict

log = logging.getLogger("smsgw")

DEFAULT_CONFIG = "/etc/smsgw/smsgw.yaml"


def build(cfg):
    sinks = build_sinks(cfg)
    store = Store(cfg.state_dir / "smsgw.db")
    return sinks, store, Gateway(cfg, store, sinks)


def run(cfg) -> int:
    sinks, store, gateway = build(cfg)
    serve(cfg, gateway)

    stop = threading.Event()

    modem_sink = next(
        (s for s in sinks.values() if getattr(s, "type", "") == "sms_modem"), None
    )
    if modem_sink:
        inbox = ModemInbox(modem_sink, gateway.on_inbound)

        def poll():
            while not stop.is_set():
                try:
                    inbox.poll_once()
                except Exception:
                    log.exception("inbound poll failed")
                stop.wait(cfg.poll_seconds)

        threading.Thread(target=poll, name="inbox", daemon=True).start()
        log.info("polling for inbound SMS every %ds", cfg.poll_seconds)
    else:
        log.warning("no sms_modem sink configured - inbound SMS disabled")

    def shutdown(signum, frame):
        log.info("signal %s, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info("smsgw ready: sinks=%s | image=%s | text=%s | forward=%d",
             ",".join(sinks),
             ">".join(cfg.routing.get("with_image", [])),
             ">".join(cfg.routing.get("text_only", [])),
             len(cfg.forward))
    while not stop.is_set():
        stop.wait(1)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="smsgw")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("check")
    sub.add_parser("status")
    p_send = sub.add_parser("send")
    p_send.add_argument("number")
    p_send.add_argument("text", nargs="+")
    p_send.add_argument("--image")
    p_send.add_argument("--force", choices=["sms", "mms"],
                        help="override transport selection")

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        cfg = cfgmod.load(args.config)
    except cfgmod.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if args.cmd == "check":
        # Validate config only - never opens the database, so it stays runnable
        # as any user and can't be blocked by the daemon's state-dir ownership.
        sinks = build_sinks(cfg)
        print(f"config OK: {args.config}")
        print(f"  http           : {cfg.http_host}:{cfg.http_port}")
        print(f"  sinks          : {', '.join(sinks)}")
        for key in ("with_image", "long_text", "text_only"):
            print(f"  route {key:<10}: {' -> '.join(cfg.routing.get(key, []))}")
        print(f"  line number    : {cfg.line_number or '(unset)'}")
        print(f"  forward to     : {', '.join(cfg.forward) or '(none)'}")
        print(f"  modem          : {cfg.modem_port} (poll {cfg.poll_seconds}s)")
        print(f"  rate ceiling   : {cfg.limits.get('max_per_hour')}/h, "
              f"{cfg.limits.get('max_per_day')}/day")
        return 0

    if args.cmd == "status":
        store = Store(cfg.state_dir / "smsgw.db")
        print(json.dumps({
            "modem": modem_status(cfg.modem_port),
            "counts": store.counts(),
            "recent": [row_to_dict(r) for r in store.since(0, 5)],
        }, indent=2, default=str))
        return 0

    if args.cmd == "send":
        _, _, gateway = build(cfg)
        result = gateway.send(args.number, " ".join(args.text), args.image,
                              force=args.force)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "sent" else 1

    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
