"""test_alerts.py — Unit tests for the alerts module (no real SMTP).

Self-contained, stdlib only. Run: python3 tests/test_alerts.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import alerts

FAILURES = []

def check(name, actual, expected):
    if actual != expected:
        FAILURES.append(f"FAIL {name}: got {actual!r}, expected {expected!r}")
    else:
        print(f"  ok  {name}")

def check_true(name, val):
    if not val:
        FAILURES.append(f"FAIL {name}: expected truthy, got {val!r}")
    else:
        print(f"  ok  {name}")

def check_false(name, val):
    if val:
        FAILURES.append(f"FAIL {name}: expected falsy, got {val!r}")
    else:
        print(f"  ok  {name}")


print("=== test_alerts.py ===")

# No SMTP host -> send returns False, no crash
print("\n[send: no smtp_host]")
ok = alerts.send("test subject", "test body",
                 smtp_host="", smtp_port=587,
                 smtp_user="", smtp_pass="", to="test@example.com")
check("no smtp_host -> False", ok, False)

# No recipient -> False
ok2 = alerts.send("subject", "body",
                  smtp_host="smtp.example.com", smtp_port=587,
                  smtp_user="", smtp_pass="", to="")
check("no recipient -> False", ok2, False)

# Cooldown: first call passes the gate, second is suppressed
print("\n[cooldown]")
alerts._last_sent.clear()
check_true("first call: cooldown ok", alerts._cooldown_ok("mykey", 300))
check_false("second call: suppressed", alerts._cooldown_ok("mykey", 300))
# Different key: not suppressed
check_true("different key: ok", alerts._cooldown_ok("otherkey", 300))
# After cooldown expires: ok again
alerts._last_sent["mykey"] = time.time() - 301
check_true("after cooldown: ok again", alerts._cooldown_ok("mykey", 300))

# alert_no_price: streak < 3 -> no send attempt (returns None silently)
print("\n[alert_no_price]")
alerts._last_sent.clear()
# streak=2 should not send; we verify by checking _last_sent is not populated
alerts.alert_no_price("simd", 2, smtp_host="", smtp_port=587, smtp_user="", smtp_pass="", to="x@y.com")
check("streak<3: no entry in _last_sent", "noprice-simd" in alerts._last_sent, False)
# streak=3 would attempt send; with no smtp_host it returns False gracefully
alerts.alert_no_price("simd", 3, smtp_host="", smtp_port=587, smtp_user="", smtp_pass="", to="x@y.com")
# no crash = ok

# alert_trade: no smtp_host -> no crash
print("\n[alert_trade / alert_error: no crash]")
alerts.alert_trade("buy", "simd", 100.0, 0.5, 50.0, None, "paper",
                   smtp_host="", smtp_port=587, smtp_user="", smtp_pass="", to="")
alerts.alert_trade("sell", "simd", 100.0, 0.52, 52.0, 4.0, "paper",
                   smtp_host="", smtp_port=587, smtp_user="", smtp_pass="", to="")
alerts.alert_error("simd", "something went wrong",
                   smtp_host="", smtp_port=587, smtp_user="", smtp_pass="", to="")
print("  ok  no crash on no-smtp calls")

print()
if FAILURES:
    for f in FAILURES:
        print(f)
    print(f"\nFAILURES: {len(FAILURES)}")
    sys.exit(1)
else:
    print("ALL OK")
    sys.exit(0)
