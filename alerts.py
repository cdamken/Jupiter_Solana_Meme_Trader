from __future__ import annotations
"""alerts.py — Email alerts for Jupiter.

Sends alerts on trades, errors, and consecutive price feed failures.
All calls are fire-and-forget: errors are logged but never re-raised so
a broken SMTP config never stops the scheduler.
"""
import logging
import smtplib
import ssl
import time
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

# Minimum seconds between two alerts of the same type (de-duplication)
_COOLDOWN_S = 300
_last_sent: dict[str, float] = {}


def _cooldown_ok(key: str, cooldown: float = _COOLDOWN_S) -> bool:
    now = time.time()
    if now - _last_sent.get(key, 0) < cooldown:
        return False
    _last_sent[key] = now
    return True


def send(subject: str, body: str, *, smtp_host: str, smtp_port: int,
         smtp_user: str, smtp_pass: str, to: str, cooldown_key: str = "",
         cooldown: float = _COOLDOWN_S) -> bool:
    """Send a plain-text email. Returns True on success."""
    if not smtp_host or not to:
        return False
    if cooldown_key and not _cooldown_ok(cooldown_key, cooldown):
        log.debug("alert suppressed (cooldown): %s", cooldown_key)
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"[Jupiter] {subject}"
        msg["From"]    = smtp_user or to
        msg["To"]      = to
        ctx = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
            s.ehlo()
            s.starttls(context=ctx)
            if smtp_user and smtp_pass:
                s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        log.info("alert sent: %s", subject)
        return True
    except Exception as e:
        log.warning("alert failed (%s): %s", subject, e)
        return False


# ---- convenience wrappers ----

def alert_trade(side: str, slug: str, tokens: float, price: float,
                usd: float, pnl_pct: float | None, mode: str, **smtp):
    pnl_str = f"  P&L:    {pnl_pct:+.1f}%\n" if pnl_pct is not None else ""
    body = (
        f"Coin:   {slug}\n"
        f"Mode:   {mode}\n"
        f"Side:   {side.upper()}\n"
        f"Tokens: {tokens:.4f}\n"
        f"Price:  ${price:.6f}\n"
        f"USD:    ${usd:.2f}\n"
        f"{pnl_str}"
        f"Time:   {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
    )
    send(
        f"{side.upper()} {slug} ${usd:.2f} @ ${price:.6f}",
        body,
        cooldown_key=f"trade-{slug}-{side}",
        cooldown=60,
        **smtp,
    )


def alert_error(slug: str, message: str, **smtp):
    body = (
        f"Coin:  {slug}\n"
        f"Error: {message}\n"
        f"Time:  {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
    )
    send(
        f"ERROR {slug}: {message[:60]}",
        body,
        cooldown_key=f"error-{slug}",
        cooldown=300,
        **smtp,
    )


def alert_no_price(slug: str, consecutive: int, **smtp):
    if consecutive < 3:
        return
    body = (
        f"Coin:        {slug}\n"
        f"Consecutive: {consecutive} ticks without a price\n"
        f"Time:        {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
        "The scheduler is skipping this coin until the feed recovers.\n"
    )
    send(
        f"No price feed: {slug} ({consecutive} ticks)",
        body,
        cooldown_key=f"noprice-{slug}",
        cooldown=1800,
        **smtp,
    )
