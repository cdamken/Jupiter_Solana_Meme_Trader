"""config.py — Load and validate configuration from .env.

Fail-closed: the scheduler refuses to start if any required key is missing or invalid.
Secrets (RPC, keypair, SMTP) live here only; they never go into the DB.
"""
import os
import pathlib


def _load_dotenv(path=".env"):
    """Minimal .env parser (no external deps). Skips comments and blank lines."""
    p = pathlib.Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

REQUIRED = ("RPC_URL", "KEYPAIR_PATH", "QUOTE_MINT")

_missing = [k for k in REQUIRED if not os.environ.get(k)]
if _missing:
    raise RuntimeError(
        f"Missing required config keys (add to .env): {', '.join(_missing)}"
    )

RPC_URL              = os.environ["RPC_URL"]
KEYPAIR_PATH         = os.environ["KEYPAIR_PATH"]
QUOTE_MINT           = os.environ["QUOTE_MINT"]   # always USDC (hard rule)
GAS_RESERVE_LAMPORTS = int(os.environ.get("GAS_RESERVE_LAMPORTS", "50000000"))
ALERT_EMAIL          = os.environ.get("ALERT_EMAIL", "")
SMTP_HOST            = os.environ.get("SMTP_HOST", "")
SMTP_PORT            = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER            = os.environ.get("SMTP_USER", "")
SMTP_PASS            = os.environ.get("SMTP_PASS", "")
DEFAULT_MODE         = os.environ.get("DEFAULT_MODE", "paper")

# Store path — override in tests via JUPITER_DB env var
DB_PATH = os.environ.get("JUPITER_DB", "store/jupiter.db")
