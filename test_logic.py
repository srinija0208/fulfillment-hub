"""
test_logic.py
=============
Checks on the rules in logic.py, run against the generated sample data.

    python test_logic.py

These are the checks that would matter in production: the data agrees with its
own clock, the SLA buckets add up, the stock maths is consistent, the action list
is genuinely ranked, and actions taken in the app move the numbers they should.
"""

from pathlib import Path

import pandas as pd

import logic

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


d = logic.load_data(Path(__file__).resolve().parent / "data")
SNAP = d["snapshot"]
o = logic.derive_orders(d["orders"], d["items"], d["couriers"], SNAP)
chk, inv = logic.stock_check(o, d["items"], d["inventory"])
blocked = logic.blocked_orders(o, d["items"], chk)
transfer = logic.transfer_needed_orders(o, d["items"], chk)
actions = logic.attention_list(o, blocked, transfer)

print("\nData integrity")
for stage in logic.STAGES[1:]:
    col = logic.STAGE_TS_COL[stage]
    check(f"no {stage} timestamp after the snapshot", bool((o[col].dropna() <= SNAP).all()))
check("no label is dated after the snapshot", bool((o["label_created_ts"].dropna() <= SNAP).all()))
check("a later stage never has an earlier timestamp",
      all((o[o[logic.STAGE_TS_COL[a]].notna() & o[logic.STAGE_TS_COL[b]].notna()]
           .eval(f"{logic.STAGE_TS_COL[a]} >= {logic.STAGE_TS_COL[b]}")).all()
          for a, b in zip(logic.STAGES[1:], logic.STAGES[:-1])))
check("a stage timestamp never exists without the stage before it",
      all(o[o[logic.STAGE_TS_COL[s]].notna()
            & o[logic.STAGE_TS_COL[logic.STAGES[i - 1]]].isna()].empty
          for i, s in enumerate(logic.STAGES) if i > 0))
check("an order's status matches its latest timestamp",
      bool((o.apply(lambda r: pd.notna(r[logic.STAGE_TS_COL[r["status"]]]), axis=1)).all()))

print("\nSLA logic")
check("shipped orders are only Met or Missed",
      set(o.loc[o.status == "Shipped", "sla_status"]) <= {"Met", "Missed"})
check("open orders are never Met/Missed",
      set(o.loc[o.status != "Shipped", "sla_status"]) <= {"On Track", "At Risk", "Breached"})
shipped = o[o.status == "Shipped"]
check("Met matches shipped_ts <= deadline",
      int((shipped.shipped_ts <= shipped.sla_deadline).sum())
      == int((shipped.sla_status == "Met").sum()))
open_orders = o[o.status != "Shipped"]
check("Breached means the deadline has already passed",
      int((open_orders.sla_deadline < SNAP).sum())
      == int((open_orders.sla_status == "Breached").sum()))
check("a realistic number of breaches (not zero, not everything)",
      0 < int((o.sla_status == "Breached").sum()) < 40,
      f"{int((o.sla_status == 'Breached').sum())} breached of {len(o)}")
due_today = open_orders[(open_orders.priority) & (open_orders.sla_deadline.dt.date == SNAP.date())]
check("priority orders due today are never sitting comfortably 'On Track'",
      set(due_today.sla_status) <= {"At Risk", "Breached"},
      f"{len(due_today)} order(s) still due today")

print("\nStock logic")
tr = chk[chk.status == "Transfer Required"]
sh = chk[chk.status == "Stock Shortage"]
check("transfer-required SKUs are short in Main but coverable from Backup",
      bool(((tr.main_available < tr.required_qty)
            & (tr.main_available + tr.backup_available >= tr.required_qty)).all()))
check("shortage SKUs cannot be covered by either warehouse",
      bool((sh.main_available + sh.backup_available < sh.required_qty).all()))
check("transfer quantity equals the Main shortfall",
      bool((tr.transfer_qty == tr.required_qty - tr.main_available).all()))
check("no SKU is both a transfer and a shortage", len(set(tr.product_id) & set(sh.product_id)) == 0)
main = inv[inv.warehouse == "Main"]
counted = main[main.counted_qty.notna()]
check("a recent physical count overrides the system quantity",
      bool((counted.effective_qty == counted.counted_qty).all()) and len(counted) > 0)

open_ids = set(o.loc[o.status == "Received", "order_id"])
expected_blocked = set(d["items"].loc[d["items"].order_id.isin(open_ids)
                                      & d["items"].product_id.isin(set(sh.product_id)), "order_id"])
check("blocked orders are exactly those needing a shortage SKU", blocked == expected_blocked,
      f"{len(blocked)} blocked, {len(transfer)} awaiting transfer")

print("\nAction list")
breached_rows = actions[actions.sla_status == "Breached"]
others = actions[actions.sla_status != "Breached"]
check("every breached order is ranked above every non-breached one",
      bool(len(breached_rows) == 0 or len(others) == 0
           or breached_rows.index.max() < others.index.min()))
check("inside the breached band, priority orders come first (the tie-break)",
      list(breached_rows.priority) == sorted(breached_rows.priority, reverse=True),
      f"{len(breached_rows)} breached rows, top item "
      f"{actions.iloc[0]['order_id'] if not actions.empty else '-'}")
check("every flagged order carries a reason and a next action",
      bool(actions["reason"].str.len().gt(0).all() and actions["next_action"].str.len().gt(0).all()))
check("the list stays short enough to act on", 0 < len(actions) <= logic.TOP_N_ACTIONS,
      f"{len(actions)} rows")
check("blocked orders always say why they are blocked",
      bool(actions[actions.order_id.isin(blocked)]["reason"].str.contains("stock short").all()))

print("\nWarehouse views")
picks = logic.pick_list(o, d["items"], chk, blocked | transfer)
check("the pick list never contains a blocked or transfer-held order",
      not ({p["order_id"] for p in picks} & (blocked | transfer)))
check("the pick list is priority-first",
      all(item["priority"] for item in picks[:sum(1 for i in picks if i["priority"])]))
check("every pick line has a shelf location",
      all(line["bin"] != "?" for item in picks for line in item["picks"]))
board = logic.staging_board(o, SNAP)
check("the staging board only contains staged orders", set(board.status) <= {"Staged"})
if not board.empty:
    check("a missed pickup is only flagged when the slot really has passed",
          bool((board[board.slot_status.str.startswith("MISSED")].courier_pickup_ts < SNAP).all()))

print("\nActions taken in the app")
if not tr.empty:
    t_sku, need = tr.iloc[0].product_id, int(tr.iloc[0].transfer_qty)
    before = int(inv[(inv.product_id == t_sku) & (inv.warehouse == "Main")].quantity.iloc[0])
    _, inv2, _ = logic.apply_events(d["orders"], d["inventory"], d["exceptions"],
                                    [{"kind": "transfer", "product_id": t_sku, "qty": need}])
    after = int(inv2[(inv2.product_id == t_sku) & (inv2.warehouse == "Main")].quantity.iloc[0])
    check("a recorded transfer adds stock to Main", after == before + need)
    chk2, _ = logic.stock_check(o, d["items"], inv2)
    check("after the transfer the SKU is OK",
          chk2.loc[chk2.product_id == t_sku, "status"].iloc[0] == "OK")

# a transfer must also clear a SKU that has a physical count on it
mismatch = main[main.counted_qty.notna()].product_id.tolist()
sku_c = [s for s in mismatch if s in set(chk.product_id)]
if sku_c:
    s = sku_c[0]
    row_c = chk[chk.product_id == s].iloc[0]
    q = max(int(row_c.required_qty - row_c.main_available), 1) if row_c.status != "OK" else 5
    _, inv_c, _ = logic.apply_events(d["orders"], d["inventory"], d["exceptions"],
                                     [{"kind": "transfer", "product_id": s, "qty": q}])
    eff_before = int(main[main.product_id == s].effective_qty.iloc[0])
    eff_after = int(inv_c[(inv_c.product_id == s) & (inv_c.warehouse == "Main")]
                    .counted_qty.iloc[0])
    check("a transfer also lifts the counted quantity (count overrides system)",
          eff_after == eff_before + q)

target = picks[0]["order_id"]
o3, _, _ = logic.apply_events(d["orders"], d["inventory"], d["exceptions"],
                              [{"kind": "advance", "order_id": target, "to": "Picked", "ts": SNAP}])
row = o3[o3.order_id == target].iloc[0]
check("marking a pick moves the order to Picked with a timestamp",
      row.status == "Picked" and pd.notna(row.picked_ts))

o3b, _, _ = logic.apply_events(d["orders"], d["inventory"], d["exceptions"],
                               [{"kind": "label", "order_id": target, "ts": SNAP}])
check("creating a label clears it from the office queue",
      pd.notna(o3b[o3b.order_id == target].label_created_ts.iloc[0]))

open_ex = d["exceptions"][d["exceptions"].status != "Resolved"].iloc[0].exception_id
_, _, ex2 = logic.apply_events(d["orders"], d["inventory"], d["exceptions"],
    [{"kind": "resolve", "exception_id": open_ex, "ts": SNAP,
      "resolution": "Fixed.", "status": "Resolved"}])
r = ex2[ex2.exception_id == open_ex].iloc[0]
check("resolving an issue closes it with a timestamp",
      r.status == "Resolved" and pd.notna(r.resolved_ts))

print("\nIssue log")
# exceptions can't pre-date the stage they belong to
anchor = {"Inventory Shortage": "received_ts", "Wrong Variant": "packed_ts",
          "Box Misplaced": "staged_ts", "Pickup Missed": "staged_ts"}
by_id = d["orders"].set_index("order_id")
ex_o = d["exceptions"].dropna(subset=["order_id"])
bad = [e.exception_id for e in ex_o.itertuples()
       if pd.notna(by_id.loc[e.order_id, anchor[e.exception_type]])
       and e.created_ts < by_id.loc[e.order_id, anchor[e.exception_type]]]
check("no exception is raised before the stage it belongs to", not bad, str(bad))

# every missed-pickup issue refers to a box staged before a slot that has passed
cr = d["couriers"].set_index("courier_id")
mp = ex_o[(ex_o.exception_type == "Pickup Missed")]
ok_mp = True
for e in mp.itertuples():
    ordr = by_id.loc[e.order_id]
    slot = logic.courier_slot(cr.loc[ordr.courier_id], "pickup_time", ordr.staged_ts)
    ok_mp &= bool(pd.notna(ordr.staged_ts) and e.created_ts >= ordr.staged_ts and slot < SNAP)
check("every Pickup Missed refers to a staged box and a courier slot that has passed", ok_mp)

# the warehouse flow can be walked end to end
oid = picks[0]["order_id"]
o4, _, _ = logic.apply_events(d["orders"], d["inventory"], d["exceptions"],
    [{"kind": "advance", "order_id": oid, "to": s, "ts": SNAP}
     for s in ("Picked", "Packed", "Staged")])
r4 = o4[o4.order_id == oid].iloc[0]
check("an order can be walked Picked -> Packed -> Staged",
      r4.status == "Staged" and pd.notna(r4.packed_ts) and pd.notna(r4.staged_ts))

# log-then-resolve doesn't break the metrics
nid = logic.next_exception_id(d["exceptions"])
_, _, ex3 = logic.apply_events(d["orders"], d["inventory"], d["exceptions"], [
    {"kind": "exception", "row": {
        "exception_id": nid, "order_id": None, "product_id": None,
        "exception_type": "Wrong Variant", "severity": "High", "owner": "Warehouse Team",
        "stage": "Packing", "description": "test", "status": "Open",
        "created_ts": SNAP, "resolved_ts": None, "resolution": "No action yet."}},
    {"kind": "resolve", "exception_id": nid, "ts": SNAP,
     "resolution": "Fixed.", "status": "Resolved"}])
m = logic.issue_metrics(ex3, SNAP)
check("logging then resolving an issue doesn't break the metrics",
      m["resolved"] == int((d["exceptions"].status == "Resolved").sum()) + 1)

new_id = logic.next_exception_id(d["exceptions"])
check("new issues get a fresh id", new_id not in set(d["exceptions"].exception_id), new_id)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    raise SystemExit(1)
