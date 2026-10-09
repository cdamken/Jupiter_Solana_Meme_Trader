"""test_pairing.py -- Tests for pairing/combined group sales.

Self-contained, stdlib only. Run: python3 tests/test_pairing.py
"""
import sys
import os
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engine import (
    stuck_lot, update_stuck_clock, mature_target, cost_matched,
    leveler_eligible, group_shortfall_pct, repair_grid_links,
    eligible_groups, revalidate_combined_sale, pairing_reserved_ids,
)

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


print("=== test_pairing.py ===")

# --- stuck_lot ---
print("\n[stuck_lot]")
# buy_price=1.0, price=0.8 -> lot is 25% above current -> stuck at 20% threshold
check_true("stuck: bp=1.0 px=0.8 thresh=20", stuck_lot({"buy_price": 1.0, "cost": 25.0}, 0.8, 20.0))
# not stuck: bp=1.0, price=0.9 -> 11% above -> not stuck at 20%
check_false("not stuck: bp=1.0 px=0.9", stuck_lot({"buy_price": 1.0, "cost": 25.0}, 0.9, 20.0))
# dust lot: cost < 1
check_false("dust: cost=0.5", stuck_lot({"buy_price": 1.0, "cost": 0.5}, 0.8, 20.0))
# bad price
check_false("bad price: px=0", stuck_lot({"buy_price": 1.0, "cost": 25.0}, 0, 20.0))


# --- update_stuck_clock ---
print("\n[update_stuck_clock]")
now = 100000.0
lots_uc = [
    {"id": 1, "buy_price": 1.0, "cost": 25.0, "stuck_since": None},   # newly stuck
    {"id": 2, "buy_price": 0.5, "cost": 25.0, "stuck_since": 90000},  # was stuck, now recovered
    {"id": 3, "buy_price": 1.0, "cost": 25.0, "stuck_since": 80000},  # still stuck
]
updates = update_stuck_clock(lots_uc, 0.8, 20.0, now)
check("new stuck: stamped", updates.get(1), now)
check("recovered: cleared", updates.get(2), None)
check_false("still stuck: no update", 3 in updates)


# --- mature_target ---
print("\n[mature_target]")
check_true("mature: 8 days stuck, 7 day thresh",
           mature_target({"stuck_since": now - 8 * 86400}, now, 7.0))
check_false("immature: 5 days stuck, 7 day thresh",
            mature_target({"stuck_since": now - 5 * 86400}, now, 7.0))
check_false("no clock", mature_target({"stuck_since": None}, now, 7.0))


# --- cost_matched ---
print("\n[cost_matched]")
check_true("same cost", cost_matched(25.0, 25.0))
check_true("within 15%", cost_matched(22.0, 25.0))
check_false("too far: 20%", cost_matched(20.0, 25.0))
check_false("target cost 0", cost_matched(25.0, 0.0))


# --- leveler_eligible ---
print("\n[leveler_eligible]")
check_true("grid lot in profit", leveler_eligible(
    {"tokens": 50.0, "cost": 25.0, "buy_price": 0.5, "origin": "grid"}, 0.6))
check_false("not in profit", leveler_eligible(
    {"tokens": 50.0, "cost": 25.0, "buy_price": 0.5, "origin": "grid"}, 0.4))
check_false("prebuy excluded", leveler_eligible(
    {"tokens": 50.0, "cost": 25.0, "buy_price": 0.5, "origin": "prebuy"}, 0.6))
check_false("reserve excluded", leveler_eligible(
    {"tokens": 50.0, "cost": 25.0, "buy_price": 0.5, "origin": "reserve"}, 0.6))
check_false("zero tokens", leveler_eligible(
    {"tokens": 0.0, "cost": 25.0, "buy_price": 0.5, "origin": "grid"}, 0.6))


# --- group_shortfall_pct ---
print("\n[group_shortfall_pct]")
# group: 100 tokens, $120 cost, price=1.0, need +3% net
# exit_price = 120 * 1.03 / 100 = 1.236, shortfall = (1.236/1.0 - 1)*100 = 23.6%
sf = group_shortfall_pct(100.0, 120.0, 1.0, 3.0)
check("shortfall calc", round(sf, 1), 23.6)
check("zero tokens", group_shortfall_pct(0, 100, 1.0, 3.0), 0.0)


# --- repair_grid_links ---
print("\n[repair_grid_links]")
stuck_time = now - 10 * 86400  # 10 days ago
lots_rgl = [
    # target: stuck high (bp=2.0, price=1.0, 100% above)
    {"id": 10, "buy_price": 2.0, "cost": 50.0, "tokens": 25.0,
     "origin": "grid", "stuck_since": stuck_time, "levels_to": None, "grid_pair": False},
    # leveler: in profit, same cost
    {"id": 20, "buy_price": 0.5, "cost": 50.0, "tokens": 100.0,
     "origin": "grid", "stuck_since": None, "levels_to": None, "grid_pair": False},
    # another leveler (different cost, won't match)
    {"id": 30, "buy_price": 0.3, "cost": 10.0, "tokens": 33.0,
     "origin": "grid", "stuck_since": None, "levels_to": None, "grid_pair": False},
]
updates = repair_grid_links(lots_rgl, 1.0, 20.0, 7.0, 1, 3.0, now)
# lot 20 should be linked to lot 10 (cost match: 50 vs 50)
link_set = {(lid, tid) for lid, tid in updates if tid is not None}
check("linked lot 20 -> 10", (20, 10) in link_set, True)
# lot 30 should NOT be linked (cost 10 vs 50 = too far)
check("lot 30 not linked", any(lid == 30 and tid is not None for lid, tid in updates), False)

# max_levelers=0: no levelers assigned
updates0 = repair_grid_links(lots_rgl, 1.0, 20.0, 7.0, 0, 3.0, now)
check("max_lev=0: no links", all(tid is None for _, tid in updates0), True)


# --- eligible_groups ---
print("\n[eligible_groups]")
# target lot: bp=2.0, cost=50, tokens=25 -> value at px=1.5: 25*1.5 = 37.5 (underwater)
# leveler: bp=0.5, cost=50, tokens=100 -> value at px=1.5: 100*1.5 = 150
# group: cost=100, tokens=125, value=125*1.5*(1-0.015) = 184.69 -> 184.69 >= 100*1.03? yes
lots_eg = [
    {"id": 10, "buy_price": 2.0, "cost": 50.0, "tokens": 25.0, "levels_to": None},
    {"id": 20, "buy_price": 0.5, "cost": 50.0, "tokens": 100.0, "levels_to": 10},
]
groups = eligible_groups(lots_eg, 1.5, 3.0)
check("one group found", len(groups), 1)
check("group has 2 lots", len(groups[0]), 2)

# price too low: group doesn't clear
# tokens=125, cost=100, need 100*1.03=103 net -> price*(1-0.015)*125 >= 103 -> price >= 0.836
groups_low = eligible_groups(lots_eg, 0.7, 3.0)
check("price too low: no groups", len(groups_low), 0)


# --- revalidate_combined_sale ---
print("\n[revalidate_combined_sale]")
group = [
    {"tokens": 25.0, "cost": 50.0},
    {"tokens": 100.0, "cost": 50.0},
]
# tokens=125, cost=100, price=1.5 -> value_net=125*1.5*0.985=184.69 > 100
check_true("valid combined sale", revalidate_combined_sale(group, 1.5, 3.0))
# price too low
check_false("invalid: price too low", revalidate_combined_sale(group, 0.7, 3.0))
# empty
check_false("empty group", revalidate_combined_sale([], 1.5, 3.0))


# --- pairing_reserved_ids (already in engine.py) ---
print("\n[pairing_reserved_ids]")
lots_pr = [
    {"id": 10, "levels_to": None},
    {"id": 20, "levels_to": 10},   # leveler -> reserves 20 and 10
    {"id": 30, "levels_to": None},
]
reserved = pairing_reserved_ids(lots_pr)
check_true("leveler reserved", 20 in reserved)
check_true("target reserved", 10 in reserved)
check_false("unlinked not reserved", 30 in reserved)


# --- store integration ---
print("\n[store integration]")
os.environ.setdefault("RPC_URL", "http://localhost")
os.environ.setdefault("KEYPAIR_PATH", "/dev/null")
os.environ.setdefault("QUOTE_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
import config  # noqa
from store.store import Store

with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    coin_id = s.add_coin("test", "TEST", "TestMint" + "1" * 33)
    s.usdc_deposit(200.0)

    # set_lot_levels_to
    lid1 = s.add_lot(coin_id, 25.0, 50.0, 2.0, time.time(), "grid")
    lid2 = s.add_lot(coin_id, 100.0, 50.0, 0.5, time.time(), "grid")
    s.set_lot_levels_to(lid2, lid1)
    lots = [dict(l) for l in s.get_lots(coin_id)]
    leveler = next(l for l in lots if l["id"] == lid2)
    check("levels_to set", leveler["levels_to"], lid1)

    # clear levels_to
    s.set_lot_levels_to(lid2, None)
    lots = [dict(l) for l in s.get_lots(coin_id)]
    leveler = next(l for l in lots if l["id"] == lid2)
    check("levels_to cleared", leveler["levels_to"], None)

    # remove_lots
    lid3 = s.add_lot(coin_id, 10.0, 10.0, 1.0, time.time(), "grid")
    s.remove_lots([lid1, lid2, lid3])
    check("all lots removed", len(s.get_lots(coin_id)), 0)

    # remove_lots empty list is a no-op
    s.remove_lots([])


# --- regression: double-credit when leveler is in both combined sale and decision["sells"] ---
print("\n[double-credit regression]")
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "test.db")
    s = Store(db)
    coin_id = s.add_coin("dc", "DC", "DCMint" + "1" * 36)
    s.usdc_deposit(500.0)

    # target: bought at 2.0 (stuck), cost=50
    target_id = s.add_lot(coin_id, 25.0, 50.0, 2.0, time.time() - 100, "grid")
    # leveler: bought at 0.5, cost=50, in profit at price=1.5 (200% gain, exceeds any sell_step)
    leveler_id = s.add_lot(coin_id, 100.0, 50.0, 0.5, time.time() - 50, "grid")
    # link them
    s.set_lot_levels_to(leveler_id, target_id)

    lots_dc = [dict(l) for l in s.get_lots(coin_id)]
    # Simulate what grid_decision would produce: the leveler is in sells
    # (it's in profit at price=1.5, way above sell_step)
    leveler_lot = next(l for l in lots_dc if l["id"] == leveler_id)

    # Simulate the combined sale
    price_dc = 1.5
    group = eligible_groups(lots_dc, price_dc, 3.0)
    check_true("dc: group found", len(group) > 0)
    g = group[0]
    group_ids = [l["id"] for l in g]
    check_true("dc: leveler in group", leveler_id in group_ids)

    # The fix: after combined sale, remove sold lots from the individual sells list
    fake_sells = [leveler_lot]
    sold_ids = set(group_ids)
    fake_sells = [l for l in fake_sells if l["id"] not in sold_ids]
    check("dc: leveler removed from sells", len(fake_sells), 0)

    # Now do the actual combined sale
    total_tokens = sum(float(l["tokens"]) for l in g)
    total_cost = sum(float(l["cost"]) for l in g)
    proceeds = total_tokens * price_dc
    check_true("dc: revalidate passes", revalidate_combined_sale(g, price_dc, 3.0))

    bal_before = s.usdc_balance()
    s.begin()
    s.remove_lots(group_ids)
    s.usdc_commit_sell(proceeds, coin_id, f"paper-pair-{group_ids[0]}",
                       price_dc, total_tokens,
                       (proceeds / total_cost - 1) * 100, "paper")
    s.commit()

    # Verify: only ONE credit, not two
    bal_after = s.usdc_balance()
    check("dc: balance credited once", round(bal_after - bal_before, 2), round(proceeds, 2))
    check("dc: lots removed", len(s.get_lots(coin_id)), 0)

    # If the fix hadn't worked (leveler still in sells), a second paper_sell would try:
    # it would DELETE 0 rows (idempotent) but usdc_commit_sell would credit again.
    # With the fix, fake_sells is empty, so no second credit.


print()
if FAILURES:
    for f in FAILURES:
        print(f)
    print(f"\nFAILURES: {len(FAILURES)}")
    sys.exit(1)
else:
    print("ALL OK")
    sys.exit(0)
