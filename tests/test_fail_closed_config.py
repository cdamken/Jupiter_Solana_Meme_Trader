"""Tests for issue #5: money-critical config must be fail-closed and explicit."""
import unittest
import sys, os

os.environ.setdefault("RPC_URL", "http://localhost")
os.environ.setdefault("KEYPAIR_PATH", "/dev/null")
os.environ.setdefault("QUOTE_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scheduler import audit_live_config, MONEY_CRITICAL_KEYS


class TestAuditLiveConfig(unittest.TestCase):

    def _full_cfg(self, **overrides):
        base = {
            "MAX_CAPITAL_USD": "200.0",
            "LOT_USD": "25.0",
            "MAX_SLIPPAGE_BPS": "150",
        }
        base.update(overrides)
        return base

    def test_all_present_passes(self):
        cfg = self._full_cfg()
        self.assertEqual(audit_live_config(cfg, "TEST"), [])

    def test_missing_key_fails(self):
        for key in MONEY_CRITICAL_KEYS:
            cfg = self._full_cfg()
            del cfg[key]
            bad = audit_live_config(cfg, "TEST")
            self.assertIn(key, bad, f"{key} should be flagged when missing")

    def test_empty_string_fails(self):
        cfg = self._full_cfg(MAX_CAPITAL_USD="")
        bad = audit_live_config(cfg, "TEST")
        self.assertIn("MAX_CAPITAL_USD", bad)

    def test_whitespace_only_fails(self):
        cfg = self._full_cfg(LOT_USD="   ")
        bad = audit_live_config(cfg, "TEST")
        self.assertIn("LOT_USD", bad)

    def test_zero_fails(self):
        cfg = self._full_cfg(MAX_SLIPPAGE_BPS="0")
        bad = audit_live_config(cfg, "TEST")
        self.assertIn("MAX_SLIPPAGE_BPS", bad)

    def test_negative_fails(self):
        cfg = self._full_cfg(LOT_USD="-10")
        bad = audit_live_config(cfg, "TEST")
        self.assertIn("LOT_USD", bad)

    def test_non_numeric_fails(self):
        cfg = self._full_cfg(MAX_CAPITAL_USD="abc")
        bad = audit_live_config(cfg, "TEST")
        self.assertIn("MAX_CAPITAL_USD", bad)

    def test_none_value_fails(self):
        cfg = self._full_cfg(MAX_SLIPPAGE_BPS=None)
        bad = audit_live_config(cfg, "TEST")
        self.assertIn("MAX_SLIPPAGE_BPS", bad)

    def test_valid_integer_strings_pass(self):
        cfg = self._full_cfg(MAX_SLIPPAGE_BPS="200", LOT_USD="30", MAX_CAPITAL_USD="500")
        self.assertEqual(audit_live_config(cfg, "TEST"), [])

    def test_multiple_missing_returns_all(self):
        cfg = {}
        bad = audit_live_config(cfg, "TEST")
        self.assertEqual(sorted(bad), sorted(MONEY_CRITICAL_KEYS))

    def test_paper_mode_not_gated(self):
        """audit_live_config is only called for live coins; paper uses defaults."""
        cfg = {}
        bad = audit_live_config(cfg, "TEST")
        self.assertTrue(len(bad) > 0)


class TestMoneyKeys(unittest.TestCase):

    def test_all_critical_keys_listed(self):
        self.assertIn("MAX_CAPITAL_USD", MONEY_CRITICAL_KEYS)
        self.assertIn("LOT_USD", MONEY_CRITICAL_KEYS)
        self.assertIn("MAX_SLIPPAGE_BPS", MONEY_CRITICAL_KEYS)

    def test_at_least_three_keys(self):
        self.assertGreaterEqual(len(MONEY_CRITICAL_KEYS), 3)


if __name__ == "__main__":
    unittest.main()
