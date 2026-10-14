"""
logic.py
========
The business rules behind Fulfillment Hub. Pure pandas, no Streamlit, so the
rules can be reasoned about and tested on their own (see test_logic.py).

Three ideas do most of the work:

1. **"Now" is a fixed snapshot.** The app never uses the wall clock. It reads
   `snapshot_ts` from data/meta.json, and the generator guarantees that every
   *event* timestamp (stages, labels, exceptions) is <= that instant. SLA
   deadlines and inbound ETAs are the only future-looking timestamps. That is
   what makes SLA buckets and "hours in stage" stable and reproducible: the same
   numbers every time you open the app, and no order that appears to have
   travelled into the future. (In production, `snapshot` would simply be
   `datetime.now()`; nothing else in this module would change.)

2. **A delay is measured against a target, not a vague feeling.** Each stage has
   a target dwell time (tighter for priority orders). An order that has been
   waiting longer than its target is *stuck*, and says so, with the hours it has
   been waiting and what should happen next.

3. **One ranked action list.** Every problem in the brief arrives at the same
   screen: an SLA breach, a stock block, a missed pickup, a stuck order, a label
   that has not been printed. Instead of scattering them across separate screens,
   they are merged into a single list where each row carries the *reason* and the
   *next action*, sorted so the most urgent thing is at the top.
"""

from __future__ import annotations

import json
from datetime import datetime, time
from pathlib import Path

import pandas as pd

# ----------------------------------------------------------------------------
# Constants - the rules, in one place
# ----------------------------------------------------------------------------

STAGES = ["Received", "Picked", "Packed", "Staged", "Shipped"]
STAGE_TS_COL = {s: f"{s.lower()}_ts" for s in STAGES}
NEXT_STAGE = dict(zip(STAGES, STAGES[1:]))

# How long an order may sit in a stage before it counts as a delay (hours).
STUCK_TARGET_HOURS = {
    "Received": {True: 1.0, False: 2.5},   # waiting for a picker
    "Picked": {True: 2.0, False: 3.0},     # waiting for packing
    "Packed": {True: 1.0, False: 2.0},     # waiting to be staged
    "Staged": {True: 6.0, False: 6.0},     # pickup logic covers this stage better
}

# How close to its deadline an order has to be before we call it "at risk".
RISK_WINDOW_HOURS = {True: 3.0, False: 12.0}

# Urgency bands. The gaps are wide on purpose so the ordering is category-first
# (a breach always outranks a delay), with priority and waiting time only used to
# break ties inside a band. Easy to explain to a warehouse team.
URGENCY = {
    "Breached": 1000,         # the promise is already broken
    "Blocked": 800,           # cannot be picked - stock is not there
    "Pickup missed": 600,     # the box is ready and the truck has gone
    "At Risk": 400,           # still savable today
    "Stuck": 200,             # sitting still for too long
    "Transfer": 100,          # needs stock moved Backup -> Main
    "Label": 50,              # office team has not printed the label yet
}
PRIORITY_WEIGHT = 20          # tie-break bump for priority orders
TOP_N_ACTIONS = 25            # the action list stays short on purpose

# The issue types the log accepts. Kept here rather than in the UI so the log
# form, the generator and any future reporting all agree on one list.
EXCEPTION_TYPES = ["Inventory Shortage", "Wrong Variant", "Box Misplaced",
                   "Pickup Missed", "Stock Discrepancy"]
EXCEPTION_OWNERS = ["Warehouse Team", "Operations Team", "Logistics Team"]


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------

def load_data(data_dir: str | Path = "data") -> dict:
    """Read the CSVs written by generate_data.py."""
    d = Path(data_dir)

    orders = pd.read_csv(
        d / "orders.csv",
        parse_dates=["received_ts", "label_created_ts", "picked_ts",
                     "packed_ts", "staged_ts", "shipped_ts", "sla_deadline"],
    )
    items = pd.read_csv(d / "order_items.csv")
    products = pd.read_csv(d / "products.csv")
    inventory = pd.read_csv(d / "inventory.csv")
    couriers = pd.read_csv(d / "couriers.csv")
    exceptions = pd.read_csv(d / "exceptions.csv", parse_dates=["created_ts", "resolved_ts"])
    inbound = pd.read_csv(d / "inbound_deliveries.csv", parse_dates=["eta_ts"])
    meta = json.loads((d / "meta.json").read_text())

    return {
        "orders": orders,
        "items": items,
        "products": products,
        "inventory": inventory,
        "couriers": couriers,
        "exceptions": exceptions,
        "inbound": inbound,
        "snapshot": pd.Timestamp(meta["snapshot_ts"]),
        "meta": meta,
    }


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def courier_slot(courier_row, column: str, day) -> pd.Timestamp:
    """'14:00' from couriers.csv -> a timestamp on a given day."""
    h, m = (int(x) for x in str(courier_row[column]).split(":"))
    return pd.Timestamp(datetime.combine(pd.Timestamp(day).date(), time(h, m)))


def courier_map(couriers: pd.DataFrame) -> dict:
    return couriers.set_index("courier_id").to_dict("index")


def stage_timestamp(row) -> pd.Timestamp:
    return row[STAGE_TS_COL[row["status"]]]


# ----------------------------------------------------------------------------
# Per-order derived fields
# ----------------------------------------------------------------------------

def derive_orders(orders: pd.DataFrame, items: pd.DataFrame,
                  couriers: pd.DataFrame, snapshot: pd.Timestamp) -> pd.DataFrame:
    """Everything the UI needs to know about an order, computed once."""
    o = orders.copy()
    c_map = courier_map(couriers)

    o["stage_ts"] = o.apply(stage_timestamp, axis=1)
    o["hours_in_stage"] = ((snapshot - o["stage_ts"]).dt.total_seconds() / 3600).round(1)

    # --- is it stuck in its current stage? ---------------------------------
    o["stage_target_hours"] = [
        STUCK_TARGET_HOURS[r["status"]][bool(r["priority"])]
        if r["status"] in STUCK_TARGET_HOURS else 0.0
        for _, r in o.iterrows()
    ]
    o["is_stuck"] = ((o["status"] != "Shipped")
                     & (o["stage_target_hours"] > 0)
                     & (o["hours_in_stage"] > o["stage_target_hours"]))

    # --- SLA ----------------------------------------------------------------
    o["hours_to_deadline"] = ((o["sla_deadline"] - snapshot)
                              .dt.total_seconds() / 3600).round(1)

    def sla(row) -> str:
        if row["status"] == "Shipped":
            return "Met" if row["shipped_ts"] <= row["sla_deadline"] else "Missed"
        if snapshot > row["sla_deadline"]:
            return "Breached"
        return ("At Risk" if row["hours_to_deadline"] <= RISK_WINDOW_HOURS[bool(row["priority"])]
                else "On Track")

    o["sla_status"] = o.apply(sla, axis=1)
    o["hours_overdue"] = (-o["hours_to_deadline"]).clip(lower=0).round(1)

    # --- staged box waiting for a courier slot that has already gone --------
    o["courier_pickup_ts"] = o.apply(
        lambda r: courier_slot(c_map[r["courier_id"]], "pickup_time", snapshot), axis=1)

    def pickup_missed(row) -> bool:
        if row["status"] != "Staged" or pd.isna(row["staged_ts"]):
            return False
        slot = courier_slot(c_map[row["courier_id"]], "pickup_time", snapshot)
        # staged in time for the slot, but the truck has been and gone
        return bool(row["staged_ts"] <= slot < snapshot)

    o["pickup_missed"] = o.apply(pickup_missed, axis=1)
    o["awaiting_label"] = o["label_created_ts"].isna()

    # --- order value: what is actually at stake -----------------------------
    line_value = (items["quantity"] * items["unit_price"]).groupby(items["order_id"]).sum()
    o["order_value"] = o["order_id"].map(line_value).fillna(0).astype(int)

    return o


# ----------------------------------------------------------------------------
# Inventory: can this order actually be picked?
# ----------------------------------------------------------------------------

def stock_check(orders: pd.DataFrame, items: pd.DataFrame,
                inventory: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Which SKUs can Main cover, for the orders that still need picking?

    Returns (per-SKU check table, inventory table with effective quantity).

    Trust rule: where a recent physical count exists it *overrides* the system
    quantity. That is the whole "the spreadsheet says we have it but the shelf is
    empty" problem, handled explicitly instead of hoped away.
    """
    inv = inventory.copy()
    inv["effective_qty"] = inv["counted_qty"].fillna(inv["quantity"]).astype(int)
    inv["count_variance"] = inv["counted_qty"] - inv["quantity"]

    wide = (inv.pivot(index="product_id", columns="warehouse", values="effective_qty")
              .fillna(0)
              .rename(columns={"Main": "main_available", "Backup": "backup_available"}))

    to_pick = orders.loc[orders["status"] == "Received", "order_id"]
    required = (items[items["order_id"].isin(to_pick)]
                .groupby("product_id")["quantity"].sum().rename("required_qty"))

    chk = pd.concat([required, wide], axis=1).reset_index()
    for col in ("required_qty", "main_available", "backup_available"):
        chk[col] = chk[col].fillna(0).astype(int)
    chk = chk[chk["required_qty"] > 0].copy()

    def status(row) -> str:
        if row["main_available"] >= row["required_qty"]:
            return "OK"
        if row["main_available"] + row["backup_available"] >= row["required_qty"]:
            return "Transfer Required"
        return "Stock Shortage"

    chk["status"] = chk.apply(status, axis=1)
    chk["transfer_qty"] = (chk["required_qty"] - chk["main_available"]).clip(lower=0)
    chk.loc[chk["status"] != "Transfer Required", "transfer_qty"] = 0

    bins = inv.pivot(index="product_id", columns="warehouse", values="bin_location")
    chk["main_bin"] = chk["product_id"].map(bins["Main"])
    chk["backup_bin"] = chk["product_id"].map(bins["Backup"])

    rank = {"Stock Shortage": 0, "Transfer Required": 1, "OK": 2}
    chk["_rank"] = chk["status"].map(rank)
    return (chk.sort_values(["_rank", "required_qty"], ascending=[True, False])
               .drop(columns="_rank")), inv


def _orders_touching(orders: pd.DataFrame, items: pd.DataFrame,
                     sku_set: set) -> set:
    open_ids = set(orders.loc[orders["status"] == "Received", "order_id"])
    lines = items[items["order_id"].isin(open_ids)]
    return set(lines.loc[lines["product_id"].isin(sku_set), "order_id"])


def blocked_orders(orders, items, sku_check) -> set:
    """Orders that cannot be picked at all right now (stock short everywhere)."""
    return _orders_touching(orders, items,
                            set(sku_check.loc[sku_check["status"] == "Stock Shortage",
                                              "product_id"]))


def transfer_needed_orders(orders, items, sku_check) -> set:
    """Orders that can be picked once stock is moved Backup -> Main."""
    return _orders_touching(orders, items,
                            set(sku_check.loc[sku_check["status"] == "Transfer Required",
                                              "product_id"]))


# ----------------------------------------------------------------------------
# The ranked action list - the heart of the app
# ----------------------------------------------------------------------------

NEXT_ACTION_FOR_STAGE = {
    "Received": "Pick now",
    "Picked": "Pack now",
    "Packed": "Move to staging",
    "Staged": "Check staging area",
}


def attention_list(o: pd.DataFrame, blocked: set, transfer: set,
                   limit: int | None = TOP_N_ACTIONS) -> pd.DataFrame:
    """Merge every problem into one ranked list, each row with reason + action."""
    rows = []

    for _, r in o[o["status"] != "Shipped"].iterrows():
        reasons: list[str] = []
        actions: list[str] = []
        score = 0

        if r["sla_status"] == "Breached":
            reasons.append(f"SLA breached {r['hours_overdue']:.0f}h ago")
            score = max(score, URGENCY["Breached"])
            actions.append("Escalate today")
        elif r["sla_status"] == "At Risk":
            reasons.append(f"{max(r['hours_to_deadline'], 0):.0f}h left on SLA")
            score = max(score, URGENCY["At Risk"])
            actions.append("Ship today")

        if r["order_id"] in blocked:
            reasons.append("Cannot pick - stock short")
            score = max(score, URGENCY["Blocked"])
            actions.append("Chase stock / tell customer")
        elif r["order_id"] in transfer:
            reasons.append("Needs transfer from Backup")
            score = max(score, URGENCY["Transfer"])
            actions.append("Move stock Backup -> Main")

        if r["pickup_missed"]:
            reasons.append(f"{r['courier_name']} pickup slot missed")
            score = max(score, URGENCY["Pickup missed"])
            actions.append("Rebook courier pickup")

        if r["is_stuck"]:
            reasons.append(f"Stuck at {r['status']} for {r['hours_in_stage']:.0f}h "
                           f"(target {r['stage_target_hours']:.0f}h)")
            score = max(score, URGENCY["Stuck"])
            actions.append(NEXT_ACTION_FOR_STAGE.get(r["status"], "Check"))

        if r["awaiting_label"]:
            reasons.append("Shipping label not created")
            score = max(score, URGENCY["Label"])
            actions.append("Create label")

        if not reasons:
            continue

        if r["priority"]:
            score += PRIORITY_WEIGHT

        rows.append({
            "order_id": r["order_id"],
            "priority": bool(r["priority"]),
            "channel": r["channel"],
            "city": r["city"],
            "status": r["status"],
            "sla_status": r["sla_status"],
            "hours_in_stage": r["hours_in_stage"],
            "order_value": r["order_value"],
            "courier_name": r["courier_name"],
            "reason": " | ".join(reasons),
            "next_action": " + ".join(dict.fromkeys(actions)),
            "_score": score,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = (df.sort_values(["_score", "priority", "hours_in_stage"],
                         ascending=[False, False, False])
            .drop(columns="_score").reset_index(drop=True))
    return df.head(limit) if limit else df


# ----------------------------------------------------------------------------
# Warehouse view
# ----------------------------------------------------------------------------

def pick_list(o: pd.DataFrame, items: pd.DataFrame, sku_check: pd.DataFrame,
              held: set, limit: int = 12) -> list:
    """The warehouse's to-do list: priority first, then oldest, with shelf bins.

    `held` = orders that must NOT be picked yet (stock short everywhere, or
    waiting for a Backup -> Main transfer). Pass `blocked | transfer`.
    """
    todo = (o[(o["status"] == "Received") & (~o["order_id"].isin(held))]
            .sort_values(["priority", "received_ts"], ascending=[False, True]).head(limit))

    bins = sku_check.set_index("product_id")["main_bin"].to_dict()
    lines_by_order = {oid: g for oid, g in items.groupby("order_id")}

    out = []
    for _, order in todo.iterrows():
        lines = lines_by_order.get(order["order_id"])
        picks = [] if lines is None else [
            {
                "qty": int(l["quantity"]),
                "name": l["product_name"],
                "variant": l["variant"],
                "bin": bins.get(l["product_id"], "?"),
            }
            for _, l in lines.iterrows()
        ]
        out.append({
            "order_id": order["order_id"],
            "priority": bool(order["priority"]),
            "picks": picks,
            "minutes_waiting": int(order["hours_in_stage"] * 60),
            "courier_name": order["courier_name"],
            "order_value": order["order_value"],
            "channel": order["channel"],
        })
    return out


def staging_board(o: pd.DataFrame, snapshot: pd.Timestamp) -> pd.DataFrame:
    """Boxes waiting in staging, with their courier slot and whether it has gone."""
    st = o[o["status"] == "Staged"].copy()
    if st.empty:
        return st
    st["pickup_slot"] = st["courier_pickup_ts"].dt.strftime("%H:%M")
    st["slot_status"] = st.apply(
        lambda r: "MISSED - truck has gone" if r["pickup_missed"]
        else ("Slot passed - next pickup tomorrow"
              if r["courier_pickup_ts"] < snapshot else "Waiting for pickup"), axis=1)
    return st.sort_values(["pickup_slot", "order_id"])


# ----------------------------------------------------------------------------
# Issue log metrics
# ----------------------------------------------------------------------------

def issue_metrics(exceptions: pd.DataFrame, snapshot: pd.Timestamp) -> dict:
    resolved = exceptions[exceptions["status"] == "Resolved"].copy()
    resolved["hours_to_resolve"] = (
        (resolved["resolved_ts"] - resolved["created_ts"]).dt.total_seconds() / 3600).round(1)
    still_open = exceptions[exceptions["status"] != "Resolved"]
    return {
        "open": int((exceptions["status"] == "Open").sum()),
        "in_progress": int((exceptions["status"] == "In Progress").sum()),
        "resolved": int((exceptions["status"] == "Resolved").sum()),
        "open_high": int(((exceptions["status"] != "Resolved")
                          & (exceptions["severity"] == "High")).sum()),
        "avg_resolve_hours": round(float(resolved["hours_to_resolve"].mean()), 1)
        if not resolved.empty else 0.0,
        "oldest_open_hours": round(float(
            ((snapshot - still_open["created_ts"]).dt.total_seconds() / 3600).max()), 1)
        if not still_open.empty else 0.0,
    }


# ----------------------------------------------------------------------------
# Actions taken in the UI
# ----------------------------------------------------------------------------
# The CSVs are seed data. Anything a user does in the app is recorded as an event
# and replayed on top of that seed, so the app never silently rewrites the file.
# In production each event would be a database write - here it keeps the demo
# honest and reversible (resetting the session restores the original snapshot).
#
# Known simplification: picking an order does not decrement on-hand stock; the
# stock check only looks at orders still waiting to be picked.

def apply_events(orders: pd.DataFrame, inventory: pd.DataFrame,
                 exceptions: pd.DataFrame, events: list) -> tuple:
    o, inv, exc = orders.copy(), inventory.copy(), exceptions.copy()

    for e in events:
        kind = e["kind"]

        if kind == "advance":
            idx = o.index[o["order_id"] == e["order_id"]]
            stage = e["to"]
            o.loc[idx, "status"] = stage
            col = STAGE_TS_COL[stage]
            if col != "received_ts":
                o.loc[idx, col] = pd.Timestamp(e["ts"])

        elif kind == "label":
            idx = o.index[o["order_id"] == e["order_id"]]
            o.loc[idx, "label_created_ts"] = pd.Timestamp(e["ts"])

        elif kind == "transfer":
            m = (inv["product_id"] == e["product_id"]) & (inv["warehouse"] == "Main")
            b = (inv["product_id"] == e["product_id"]) & (inv["warehouse"] == "Backup")
            inv.loc[m, "quantity"] = inv.loc[m, "quantity"] + e["qty"]
            inv.loc[m, "counted_qty"] = inv.loc[m, "counted_qty"] + e["qty"]   # NaN stays NaN
            inv.loc[b, "quantity"] = inv.loc[b, "quantity"] - e["qty"]

        elif kind == "exception":
            # drop all-empty fields first (resolved_ts, order_id...) so pandas doesn't
            # guess the column type from a None; missing columns are filled with NaN/NaT
            new_row = pd.DataFrame([e["row"]]).dropna(axis=1, how="all")
            exc = pd.concat([exc, new_row], ignore_index=True)

        elif kind == "resolve":
            idx = exc.index[exc["exception_id"] == e["exception_id"]]
            new_status = e.get("status", "Resolved")
            exc.loc[idx, "status"] = new_status
            exc.loc[idx, "resolution"] = e["resolution"]
            if new_status == "Resolved":
                exc.loc[idx, "resolved_ts"] = pd.Timestamp(e["ts"])

    for col in ("created_ts", "resolved_ts"):
        exc[col] = pd.to_datetime(exc[col])
    return o, inv, exc


def next_exception_id(exceptions: pd.DataFrame) -> str:
    nums = exceptions["exception_id"].astype(str).str.extract(r"(\d+)")[0].dropna().astype(int)
    return f"EX{nums.max() + 1:03d}" if len(nums) else "EX001"
