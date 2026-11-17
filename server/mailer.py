"""
Outgoing mail for account verification and password reset.

Mailer.send(to, subject, text) is the whole interface. With no MAIL_PROVIDER the
mailer is NullMailer: nothing is sent and nothing sensitive is logged (a public deploy without a
real sender must not leak single-use links into its logs). ConsoleMailer prints the message, link
and token included, and is used only when asked for explicitly with MAIL_PROVIDER=console (laptop
or test run, never a public deploy).

Seam for a real sender: subclass Mailer, then register it in _SENDERS under a MAIL_PROVIDER name
(an SMTP or HTTPS-API sender goes here; its credentials come from MAIL_* env). get_mailer() picks it.
An unknown MAIL_PROVIDER, or MAIL_* set with no provider, raises: a misconfigured prod must not
quietly fall back.

Env: MAIL_PROVIDER (name in _SENDERS, or "console"; unset = nothing is sent), MAIL_FROM (sender
address, for real senders).
"""

import logging
import os

logger = logging.getLogger("rabble.mailer")


class Mailer:
    def send(self, to: str, subject: str, text: str) -> None:
        raise NotImplementedError


class NullMailer(Mailer):
    """The default: drops the message. Logs that mail is off, never the message or its link."""

    def send(self, to: str, subject: str, text: str) -> None:
        logger.warning("mail not sent (no MAIL_PROVIDER set): subject=%s", subject)


class ConsoleMailer(Mailer):
    """Explicit opt-in only (MAIL_PROVIDER=console). Prints the whole message, token link included."""

    def __init__(self):
        self.outbox: list[dict] = []   # tests read the last message here

    def send(self, to: str, subject: str, text: str) -> None:
        self.outbox.append({"to": to, "subject": subject, "text": text})
        del self.outbox[:-200]
        print(f"[mail:console] to={to} subject={subject}\n{text}", flush=True)


_SENDERS: dict[str, type[Mailer]] = {}   # "smtp": SmtpMailer, ... (registered by the sender's module)
_console = ConsoleMailer()
_null = NullMailer()
_override: Mailer | None = None          # tests


def set_mailer(m: Mailer | None) -> None:
    global _override
    _override = m


def get_mailer() -> Mailer:
    if _override is not None:
        return _override
    provider = os.getenv("MAIL_PROVIDER", "").strip().lower()
    if not provider:
        if any(k.startswith("MAIL_") and k != "MAIL_PROVIDER" and os.getenv(k) for k in os.environ):
            raise RuntimeError("MAIL_* is set but MAIL_PROVIDER is not; refusing to fall back to the console mailer")
        return _null
    if provider == "console":
        return _console
    cls = _SENDERS.get(provider)
    if cls is None:
        raise RuntimeError(f"unknown MAIL_PROVIDER {provider!r}")
    return cls()
