"""Configuration.

Deliberately small: the gateway is a transport, not a rules engine. Anything that
decides *when* to send lives in whatever calls the webhook.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Email-to-MMS gateway domains. Sending to <number>@<domain> with a JPEG attached
# arrives on the handset as a normal MMS. Verizon shut its gateway down.
CARRIER_MMS_DOMAINS = {
    "tmobile": "tmomail.net",
    "att": "mms.att.net",
    "sprint": "pm.sprint.com",
    "uscellular": "mms.uscc.net",
}


class ConfigError(Exception):
    pass


@dataclass
class Recipient:
    """A destination. Built on the fly from whatever number the caller passed."""
    number: str
    carrier: str = "tmobile"

    @property
    def digits(self) -> str:
        d = re.sub(r"\D", "", self.number)
        return d[-10:] if len(d) > 10 else d

    @property
    def e164(self) -> str:
        d = re.sub(r"\D", "", self.number)
        if len(d) == 10:
            return f"+1{d}"
        if len(d) == 11 and d.startswith("1"):
            return f"+{d}"
        return self.number if self.number.startswith("+") else f"+{d}"

    def mms_address(self) -> str:
        domain = CARRIER_MMS_DOMAINS.get(self.carrier)
        if not domain:
            raise ConfigError(
                f"no email-MMS gateway known for carrier {self.carrier!r} "
                f"(have: {', '.join(sorted(CARRIER_MMS_DOMAINS))})"
            )
        return f"{self.digits}@{domain}"


@dataclass
class Config:
    path: Path
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    default_carrier: str = "tmobile"
    sinks: list[dict] = field(default_factory=list)
    # Routing is per content shape, not one flat chain: a picture and a bare
    # 20-character alert want different transports, and the caller should not
    # have to know that.
    routing: dict[str, list[str]] = field(default_factory=dict)
    line_number: str = ""
    forward: list[str] = field(default_factory=list)
    limits: dict = field(default_factory=dict)
    state_dir: Path = Path("/var/lib/smsgw")
    modem_port: str = "/dev/sim7600-at"
    poll_seconds: int = 20

    def recipient(self, number: str, carrier: str | None = None) -> Recipient:
        return Recipient(number=number, carrier=carrier or self.default_carrier)


def load(config_path: str | Path) -> Config:
    config_path = Path(config_path)
    if not config_path.is_file():
        raise ConfigError(f"config not found: {config_path}")
    raw = yaml.safe_load(config_path.read_text()) or {}

    cfg = Config(path=config_path)
    http = raw.get("http") or {}
    cfg.http_host = http.get("host", "0.0.0.0")
    cfg.http_port = int(http.get("port", 8080))

    cfg.default_carrier = raw.get("default_carrier", "tmobile")
    cfg.sinks = list(raw.get("sinks") or [])
    cfg.line_number = (raw.get("line") or {}).get("number", "")

    routing = raw.get("routing") or {}
    if isinstance(routing, list):
        # Older single-chain form: apply it to every content shape.
        routing = {k: list(routing) for k in ("with_image", "text_only", "long_text")}
    cfg.routing = {k: list(v) for k, v in routing.items()}
    for key in ("with_image", "text_only", "long_text"):
        if not cfg.routing.get(key):
            raise ConfigError(f"routing.{key} must list at least one sink")
    cfg.forward = list(raw.get("forward") or [])
    cfg.limits = dict(raw.get("limits") or {})
    if raw.get("state_dir"):
        cfg.state_dir = Path(raw["state_dir"])

    modem = raw.get("modem") or {}
    cfg.modem_port = modem.get("port", "/dev/sim7600-at")
    cfg.poll_seconds = int(modem.get("poll_seconds", 20))

    if not cfg.sinks:
        raise ConfigError("no sinks defined - nothing could ever be sent")
    known = {s.get("name") for s in cfg.sinks}
    for key, chain in cfg.routing.items():
        for name in chain:
            if name not in known:
                raise ConfigError(f"routing.{key} references unknown sink {name!r}")

    return cfg


def read_secret(path: str) -> str:
    """Read a secret file, refusing it if group or world can read it."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"secret file not found: {p}")
    if p.stat().st_mode & 0o077:
        raise ConfigError(
            f"{p} is readable by group/other (mode {oct(p.stat().st_mode & 0o777)}); "
            f"run: chmod 600 {p}"
        )
    return p.read_text().strip()
