"""
generate_data.py
================
Sample dataset generator for **Fulfillment Hub** (XYZ Fulfilment).

Everything downstream reads from the CSV files produced here, so the data is
built to be *internally consistent* and to contain a few realistic, demonstrable
problem cases.

    python generate_data.py

Outputs (written to ./data):
    products.csv            what XYZ sells across its channels
    inventory.csv           on-hand stock per SKU per warehouse (Main / Backup)
    couriers.csv            courier options: cost, transit time, label cutoff, pickup time
    orders.csv              one row per order, with a timestamp per fulfilment stage
    order_items.csv         line items (what actually has to be picked)
    exceptions.csv          the issue log (shortage, wrong variant, misplaced box, missed pickup)
    inbound_deliveries.csv  supplier deliveries: in transit -> awaiting check -> put away
    meta.json               the snapshot instant the app treats as "now"

-------------------------------------------------------------------------------
DESIGN NOTES (the part a reviewer should read)
-------------------------------------------------------------------------------
1. A single fixed SNAPSHOT instant (Tue 06 Oct 2026, 18:00) is the app's "now".
   Every *event* timestamp (received -> shipped, label, exceptions) is guaranteed
   to be <= SNAPSHOT, so the dashboard never shows an order that "reached" a stage
   in the future and never has to guess what "now" is. (SLA deadlines and inbound
   ETAs are future-looking by nature and are the only timestamps that may be later.)
   That is what makes the SLA buckets (On Track / At Risk / Breached / Met /
   Missed) trustworthy and reproducible.

2. Order progress is *simulated forward through the real flow* (Received ->
   Picked -> Packed -> Staged -> Shipped) with plausible per-stage durations,
   then truncated at SNAPSHOT. An order's status and its timestamps always agree
   with each other and with how much of the day had elapsed when it arrived:
   an order received at 17:40 is never shown as shipped.

3. The dataset seeds the situations XYZ actually struggles with, so the app can
   be demonstrated end to end instead of hoping random noise produces them:
       - a 14-order overnight backlog            -> genuine SLA breaches
       - stopped-in-stage morning orders         -> delay detection
       - 3 SKUs needing a Backup -> Main transfer, 3 genuinely short
       - 2 SKUs where the physical count disagrees with the system count
       - staged boxes whose courier slot has already passed
       - inbound stock that will clear a shortage once it is put away

4. KNOWN SIMPLIFICATION: couriers are assigned at random within the allowed set
   (priority -> express only). Shipped times are not aligned to courier pickup
   slots, and the label cutoff is displayed but not enforced. A production system
   would pick the courier from the label time and cutoff.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# =============================================================================
# 0. Configuration & reproducibility
# =============================================================================

SEED = 42
random.seed(SEED)
np.random.seed(SEED)

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)

SNAPSHOT = datetime(2026, 10, 6, 18, 0)   # "now" for the whole application
TODAY = SNAPSHOT.date()
YESTERDAY = TODAY - timedelta(days=1)

N_ORDERS_TODAY = 250                      # XYZ's typical volume (200-300/day)
N_BACKLOG = 14                            # yesterday's orders that never got out

STAGES = ["Received", "Picked", "Packed", "Staged", "Shipped"]
DAY_START = datetime.combine(TODAY, time(8, 30))
DAY_SPAN_MINUTES = 555                    # order intake window 08:30 -> 17:45


def ts_today(hhmm: str) -> datetime:
    """'14:00' -> datetime on TODAY at 14:00."""
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(TODAY, time(h, m))


# =============================================================================
# 1. Products (SKUs)
# =============================================================================

PRODUCT_CATALOG = [                       # name, category, price (INR)
    ("Plate 6in", "Plates", 129),
    ("Plate 8in", "Plates", 179),
    ("Plate 12in", "Plates", 249),
    ("Bowl 5in", "Bowls", 149),
    ("Bowl 7in", "Bowls", 199),
    ("Bowl 9in", "Bowls", 279),
    ("Tray 22x12", "Trays", 349),
    ("Tray Round 9in", "Trays", 299),
    ("Tray Heart 6in", "Trays", 189),
    ("Cup 200ml", "Cups", 119),
    ("Cup 300ml", "Cups", 149),
    ("Fork", "Cutlery", 79),
    ("Spoon", "Cutlery", 79),
    ("Knife", "Cutlery", 89),
]

# Variants are category-specific. This is what makes "wrong variant shipped"
# believable: "Plate 8in Heart" and "Plate 8in Round" look the same on a shelf
# label but are different SKUs.
CATEGORY_VARIANTS = {
    "Plates": ["Round", "Square", "Rectangular", "Heart"],
    "Bowls": ["Round", "Square", "Heart"],
    "Trays": ["Rectangular", "Round", "Heart"],
    "Cups": ["Regular", "Tall"],
    "Cutlery": ["Plain", "Polished", "Gold"],
}

products = []
for idx, (name, category, price) in enumerate(PRODUCT_CATALOG, start=1):
    for variant in CATEGORY_VARIANTS[category][: random.randint(1, 3)]:
        products.append(
            {
                "product_id": f"P{idx:02d}-{variant[:3].upper()}",
                "product_name": name,
                "variant": variant,
                "category": category,
                "unit_price": price,
            }
        )

products_df = pd.DataFrame(products)


# =============================================================================
# 2. Couriers
# =============================================================================
# Four options with genuinely different trade-offs, so "which courier?" is a
# real decision:
#
#   courier            cost    transit  label cutoff  pickup   used for
#   Speedex            INR 60   1 day    12:30         14:00    priority orders
#   Metro Logistics    INR 52   2 days   14:30         16:00    express cover
#   ShipFast           INR 45   2 days   15:30         17:00    regular orders
#   ValueCourier       INR 30   3 days   17:00         18:30    cheapest, slowest

couriers = [
    ("C1", "Speedex", "Express", 60, 1, "12:30", "14:00", "Fastest. Held for priority orders."),
    ("C2", "Metro Logistics", "Express", 52, 2, "14:30", "16:00", "Mid-cost cover when Speedex is full."),
    ("C3", "ShipFast", "Standard", 45, 2, "15:30", "17:00", "Default for regular orders."),
    ("C4", "ValueCourier", "Economy", 30, 3, "17:00", "18:30", "Cheapest. Only option left after 15:30."),
]

couriers_df = pd.DataFrame(
    couriers,
    columns=["courier_id", "courier_name", "service", "cost_per_shipment",
             "speed_days", "label_cutoff", "pickup_time", "note"],
)

PRIORITY_COURIERS = ["C1", "C2"]      # office-team rule: urgent orders go express
REGULAR_COURIERS = ["C1", "C2", "C3", "C4"]
COURIER_LOOKUP = couriers_df.set_index("courier_id").to_dict("index")


# =============================================================================
# 3. Orders + order items
# =============================================================================

CHANNELS = ["Amazon", "Flipkart", "Own Website"]
CHANNEL_WEIGHTS = [0.45, 0.33, 0.22]

FIRST_NAMES = ["Aarav", "Diya", "Ishaan", "Meera", "Rohan", "Ananya", "Kabir", "Nisha",
               "Vikram", "Sneha", "Arjun", "Priya", "Dev", "Kavya", "Rahul", "Tanvi"]
LAST_NAMES = ["Sharma", "Reddy", "Iyer", "Patel", "Nair", "Gupta", "Menon", "Rao",
              "Singh", "Kulkarni", "Das", "Bose", "Verma", "Pillai"]
CITIES = ["Bengaluru", "Hyderabad", "Chennai", "Mumbai", "Pune", "Delhi",
          "Kochi", "Coimbatore", "Ahmedabad", "Jaipur", "Nagpur", "Indore"]

# Minutes spent in each stage (min, max) - a realistic same-day operation.
GAP_MINUTES = {
    "Picked": (12, 50),
    "Packed": (15, 45),
    "Staged": (5, 30),
    "Shipped": (25, 120),
}


def intent_probabilities(available_hours: float) -> list[float]:
    """How far an order gets, given how long it has been in the building.

    An order that arrived at 08:45 has had most of the day and is usually gone;
    one that arrived 40 minutes ago realistically has not left yet. This is what
    keeps the order funnel believable instead of uniformly random.
    """
    if available_hours >= 6:
        return [0.03, 0.04, 0.05, 0.08, 0.80]
    if available_hours >= 3:
        return [0.05, 0.07, 0.10, 0.13, 0.65]
    if available_hours >= 1.5:
        return [0.10, 0.14, 0.18, 0.20, 0.38]
    return [0.38, 0.30, 0.16, 0.11, 0.05]


def simulate_stages(received: datetime, intent: int) -> dict[str, datetime]:
    """Walk the flow forward from `received` and stop at `intent`.

    If the order is meant to be Shipped the gaps are compressed so the journey
    finishes before SNAPSHOT - nothing ships "after now".
    """
    gaps = [random.randint(*GAP_MINUTES[s]) for s in STAGES[1: intent + 1]]

    if intent == 4:
        available = (SNAPSHOT - received).total_seconds() / 60
        total = sum(gaps) or 1
        budget = max(available - 5, 15)
        if total > budget:
            scale = budget / total
            gaps = [max(3, g * scale) for g in gaps]

    stamps = {"Received": received}
    t = received
    for stage, gap in zip(STAGES[1: intent + 1], gaps):
        t = t + timedelta(minutes=gap)
        stamps[stage] = t
    return stamps


def sla_deadline_for(received: datetime, priority: bool) -> datetime:
    """Priority = same-day dispatch promise (out by 20:00). Regular = 24 hours."""
    if priority:
        return datetime.combine(received.date(), time(20, 0))
    return received + timedelta(hours=24)


def make_order(order_id: str, received: datetime, priority: bool,
               forced_intent: int | None = None,
               force_late: bool = False) -> tuple[dict, list[dict]]:
    """Build one order row plus its line items."""

    available_hours = (SNAPSHOT - received).total_seconds() / 3600

    if forced_intent is not None:
        intent = forced_intent
    else:
        intent = int(np.random.choice([0, 1, 2, 3, 4],
                                      p=intent_probabilities(available_hours)))

    stamps = simulate_stages(received, intent)

    # Safety net: never let a stage timestamp run past the snapshot.
    while intent > 0 and stamps[STAGES[intent]] > SNAPSHOT:
        del stamps[STAGES[intent]]
        intent -= 1

    status = STAGES[intent]

    if force_late:                       # shipped, but after the promised date
        deadline = sla_deadline_for(received, priority)
        ship_ts = min(deadline + timedelta(hours=random.uniform(1.5, 5)),
                      SNAPSHOT - timedelta(minutes=20))
        offset = (ship_ts - stamps["Received"]).total_seconds() / 60
        for i, stage in enumerate(STAGES[1:], start=1):
            stamps[stage] = received + timedelta(minutes=offset * i / 4)
        status = "Shipped"

    courier_id = random.choice(PRIORITY_COURIERS if priority else REGULAR_COURIERS)

    # The office team prints a label shortly after reviewing the order. An order
    # still in "Received" may not have been reviewed yet - that is the office
    # team's queue.
    label_created_ts = None
    if status != "Received":
        label_created_ts = min(
            received + timedelta(minutes=random.randint(15, 70)), SNAPSHOT)
    elif random.random() < 0.45:
        label_created_ts = min(
            received + timedelta(minutes=random.randint(15, 70)), SNAPSHOT)

    order = {
        "order_id": order_id,
        "channel": random.choices(CHANNELS, weights=CHANNEL_WEIGHTS)[0],
        "customer_name": f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}",
        "city": random.choice(CITIES),
        "priority": priority,
        "status": status,
        "received_ts": stamps.get("Received"),
        "label_created_ts": label_created_ts,
        "picked_ts": stamps.get("Picked"),
        "packed_ts": stamps.get("Packed"),
        "staged_ts": stamps.get("Staged"),
        "shipped_ts": stamps.get("Shipped"),
        "courier_id": courier_id,
        "courier_name": COURIER_LOOKUP[courier_id]["courier_name"],
        "warehouse": "Main",                       # XYZ ships only from Main
        "sla_deadline": sla_deadline_for(received, priority),
    }

    n_lines = int(np.random.choice([1, 2, 3], p=[0.60, 0.30, 0.10]))
    items = [
        {
            "order_id": order_id,
            "product_id": row["product_id"],
            "quantity": int(np.random.choice([1, 2], p=[0.75, 0.25])),
        }
        for _, row in products_df.sample(n_lines).iterrows()
    ]
    return order, items


orders: list[dict] = []
order_items: list[dict] = []
counter = 1

# --- 3a. Yesterday's backlog -------------------------------------------------
# Orders that should already be gone. They are why SLA breaches exist at all,
# and they are the first thing an ops person needs to see.
BACKLOG_PENDING = 8
BACKLOG_LATE = N_BACKLOG - BACKLOG_PENDING

backlog_rows = []
for _ in range(BACKLOG_PENDING):
    backlog_rows.append((
        datetime.combine(YESTERDAY, time(random.randint(9, 16), random.randint(0, 59))),
        random.random() < 0.35, random.choice([0, 1, 1, 2]), False,
    ))
for _ in range(BACKLOG_LATE):
    backlog_rows.append((
        datetime.combine(YESTERDAY, time(random.randint(9, 14), random.randint(0, 59))),
        random.random() < 0.25, 4, True,
    ))

for received, priority, intent, late in sorted(backlog_rows):
    order, items = make_order(f"ORD{counter:04d}", received, priority,
                              forced_intent=intent, force_late=late)
    counter += 1
    orders.append(order)
    order_items.extend(items)

# --- 3b. Today's orders ------------------------------------------------------
for _ in range(N_ORDERS_TODAY):
    frac = float(np.random.beta(2.2, 2.0))
    received = DAY_START + timedelta(minutes=int(frac * DAY_SPAN_MINUTES))
    priority = random.random() < 0.18
    available_hours = (SNAPSHOT - received).total_seconds() / 3600

    forced_intent = None
    if available_hours >= 4 and random.random() < 0.07:
        # a genuine straggler: plenty of time available, still untouched
        forced_intent = random.choice([0, 1])

    order, items = make_order(f"ORD{counter:04d}", received, priority,
                              forced_intent=forced_intent)
    counter += 1
    orders.append(order)
    order_items.extend(items)

orders_df = pd.DataFrame(orders).sort_values("received_ts").reset_index(drop=True)
order_items_df = pd.DataFrame(order_items).merge(
    products_df[["product_id", "product_name", "variant", "unit_price"]],
    on="product_id", how="left",
)

# =============================================================================
# 4. Inventory  (Main / Backup, bin locations, physical counts)
# =============================================================================
# Orders can only be picked from Main, so the app has to answer:
#   1. can Main cover the orders waiting to be picked?
#   2. if not, can Backup cover the gap (-> transfer)?
#   3. if a recent physical count disagrees with the system quantity, which do
#      we trust when promising stock? (we trust the count)


def bin_location(warehouse: str, i: int) -> str:
    if warehouse == "Main":
        return f"{'ABCD'[i % 4]}-{1 + i % 4:02d}-{1 + (i * 3) % 5:02d}"
    return f"BK-{'XYZ'[i % 3]}-{1 + i % 3:02d}"


main_qty = {row["product_id"]: random.randint(12, 90) for _, row in products_df.iterrows()}
backup_qty = {row["product_id"]: random.randint(0, 45) for _, row in products_df.iterrows()}

# --- seed the demonstrable cases on the SKUs the open orders actually need ----
open_orders = set(orders_df.loc[orders_df["status"] == "Received", "order_id"])
required = (
    order_items_df[order_items_df["order_id"].isin(open_orders)]
    .groupby("product_id")["quantity"].sum().sort_values(ascending=False)
)
required_skus = list(required.index)

TRANSFER_SKUS = required_skus[:3]     # Main short -> Backup can cover
SHORTAGE_SKUS = required_skus[3:6]    # nobody can cover (until inbound lands)

for sku in TRANSFER_SKUS:
    need = int(required[sku])
    main_qty[sku] = max(0, need - random.randint(1, 3))
    backup_qty[sku] = need + random.randint(1, 6)

for sku in SHORTAGE_SKUS:
    main_qty[sku] = 0
    backup_qty[sku] = 0

# A few unrelated SKUs live only in Backup (space pressure in Main).
spare = [s for s in products_df["product_id"] if s not in set(required_skus)]
for sku in random.sample(spare, 3):
    main_qty[sku] = 0
    backup_qty[sku] = random.randint(5, 25)

counted = {sku: None for sku in products_df["product_id"]}
count_dates = {sku: None for sku in products_df["product_id"]}
# Two SKUs where a physical count found less on the shelf than the system claims.
mismatch_skus = [s for s in products_df["product_id"] if main_qty[s] > 6][:2]
for sku in mismatch_skus:
    counted[sku] = max(0, main_qty[sku] - random.randint(3, 8))
    count_dates[sku] = (SNAPSHOT - timedelta(days=1)).strftime("%Y-%m-%d")

inventory = []
for i, row in products_df.iterrows():
    sku = row["product_id"]
    for wh, qty in (("Main", main_qty[sku]), ("Backup", backup_qty[sku])):
        inventory.append({
            "product_id": sku,
            "warehouse": wh,
            "bin_location": bin_location(wh, i),
            "quantity": qty,
            "counted_qty": counted[sku] if wh == "Main" else None,
            "count_date": count_dates[sku] if wh == "Main" else None,
            "reorder_level": 15,
        })
inventory_df = pd.DataFrame(inventory)

inv_effective = inventory_df.copy()
inv_effective["effective"] = inv_effective["counted_qty"].fillna(inv_effective["quantity"])
eff = inv_effective.pivot(index="product_id", columns="warehouse", values="effective").fillna(0)

# =============================================================================
# 5. Inbound deliveries  (unload -> check -> put away)
# =============================================================================
# Seeded so two shortage SKUs have stock landing soon: the honest answer to
# "we are short" is often "wait for the truck", and the app should say when it
# arrives instead of leaving the team guessing.

suppliers = ["Anand Ceramics", "Sri Balaji Plastics", "Nova Kitchenware", "Kochi Steel Works"]
inbound = []
for n, sku in enumerate(SHORTAGE_SKUS[:2] + random.sample(spare, 4), start=1):
    if sku in SHORTAGE_SKUS[:2]:
        eta = SNAPSHOT + timedelta(hours=random.choice([2, 4, 20]))
        status = "In Transit"
        qty = int(required[sku]) + random.randint(10, 40)
    else:
        eta = SNAPSHOT - timedelta(hours=random.choice([1, 3, 5]))
        status = random.choice(["Received - Awaiting Check", "Put Away"])
        qty = random.randint(20, 60)
    inbound.append({
        "inbound_id": f"IN{n:03d}",
        "supplier": random.choice(suppliers),
        "product_id": sku,
        "quantity": qty,
        "eta_ts": eta,
        "status": status,
    })
inbound_df = pd.DataFrame(inbound)

# =============================================================================
# 6. Exceptions / issue log
# =============================================================================
# Generated from real conditions in the data above, not at random, so every
# logged issue can be traced back to the order (and the SKU) that caused it.

shortage_order_ids = []
for oid in orders_df.loc[orders_df["status"] == "Received", "order_id"]:
    lines = order_items_df[order_items_df["order_id"] == oid]
    blocked = any(
        (eff.loc[line["product_id"], "Main"] < line["quantity"]
         if line["product_id"] in eff.index else True)
        for _, line in lines.iterrows()
    )
    if blocked:
        shortage_order_ids.append(oid)


def pickup_passed(row) -> bool:
    """Box was staged in time for today's slot, but the truck has been and gone."""
    if pd.isna(row["staged_ts"]):
        return False
    slot = ts_today(COURIER_LOOKUP[row["courier_id"]]["pickup_time"])
    return bool(row["staged_ts"] <= slot < SNAPSHOT)


staged = orders_df[orders_df["status"] == "Staged"].copy()
missed_pickup_ids = staged[staged.apply(pickup_passed, axis=1)]["order_id"].tolist()

EXCEPTION_RULES = {
    "Inventory Shortage": ("High", "Warehouse Team", "Picking",
                           "Required stock is not on the shelf in Main."),
    "Wrong Variant": ("High", "Warehouse Team", "Packing",
                      "Packed item does not match the variant on the order."),
    "Box Misplaced": ("Medium", "Warehouse Team", "Staging",
                      "Packed order could not be found in staging."),
    "Pickup Missed": ("High", "Logistics Team", "Staging",
                      "Courier did not collect the box during the pickup slot."),
    "Stock Discrepancy": ("Medium", "Operations Team", "Inventory",
                          "Physical count is lower than the system quantity."),
}

RESOLUTIONS = {
    "Inventory Shortage": "Stock transferred from Backup warehouse.",
    "Wrong Variant": "Correct variant re-picked and verified.",
    "Box Misplaced": "Box located, moved back to staging.",
    "Pickup Missed": "Pickup rebooked for the next slot.",
    "Stock Discrepancy": "System quantity corrected after physical count.",
}

exceptions = []
ex_counter = 1


def add_exception(order_id, etype, created, status, product_id=None):
    global ex_counter
    severity, owner, stage, description = EXCEPTION_RULES[etype]
    resolved_ts = None
    if status == "Resolved":
        resolved_ts = min(created + timedelta(hours=random.uniform(0.5, 4)), SNAPSHOT)
        resolution = RESOLUTIONS[etype]
    elif status == "In Progress":
        resolution = "Being looked into."
    else:
        resolution = "No action yet."

    exceptions.append({
        "exception_id": f"EX{ex_counter:03d}",
        "order_id": order_id,
        "product_id": product_id,
        "exception_type": etype,
        "severity": severity,
        "owner": owner,
        "stage": stage,
        "description": description,
        "status": status,
        "created_ts": created,
        "resolved_ts": resolved_ts,
        "resolution": resolution,
    })
    ex_counter += 1


def PICK():
    return random.choices(["Open", "In Progress", "Resolved"], [0.35, 0.4, 0.25])[0]


# 6a. Inventory shortages - raised when the picker reaches the shelf
for oid in shortage_order_ids[:5]:
    row = orders_df.loc[orders_df["order_id"] == oid].iloc[0]
    created = min(row["received_ts"] + timedelta(minutes=random.randint(45, 150)),
                  SNAPSHOT - timedelta(minutes=30))
    add_exception(oid, "Inventory Shortage", created, PICK())

# 6b. Wrong variant - caught at packing
for _, row in orders_df[orders_df["status"].isin(["Packed", "Staged", "Shipped"])].sample(3).iterrows():
    created = (row["packed_ts"] or row["received_ts"]) + timedelta(minutes=random.randint(5, 35))
    add_exception(row["order_id"], "Wrong Variant",
                  min(created, SNAPSHOT), PICK())

# 6c. Boxes misplaced in staging
for _, row in staged.sample(min(2, len(staged))).iterrows():
    created = row["staged_ts"] + timedelta(minutes=random.randint(25, 70))
    add_exception(row["order_id"], "Box Misplaced",
                  min(created, SNAPSHOT), PICK())

# 6d. Missed pickups - exactly the staged boxes whose slot has passed
for oid in missed_pickup_ids[:6]:
    row = orders_df.loc[orders_df["order_id"] == oid].iloc[0]
    created = ts_today(COURIER_LOOKUP[row["courier_id"]]["pickup_time"]) + \
        timedelta(minutes=random.randint(20, 60))
    add_exception(oid, "Pickup Missed", min(created, SNAPSHOT), PICK())

# 6e. Stock counts that did not match the spreadsheet
for sku in mismatch_skus:
    created = datetime.combine(YESTERDAY, time(random.randint(15, 17), 30))
    add_exception(None, "Stock Discrepancy", created,
                  random.choice(["In Progress", "Resolved"]), product_id=sku)

# 6f. Backlog history. Each issue is raised AFTER the stage it belongs to, so it
#     can never pre-date the order or the box it refers to.
ANCHOR = {"Inventory Shortage": "received_ts", "Wrong Variant": "packed_ts",
          "Box Misplaced": "staged_ts", "Pickup Missed": "staged_ts"}
backlog = orders_df[orders_df["received_ts"].dt.date == YESTERDAY]
for etype in ["Wrong Variant", "Box Misplaced", "Inventory Shortage",
              "Pickup Missed", "Wrong Variant"]:
    pool = backlog[backlog[ANCHOR[etype]].notna()]
    row = pool.sample(1).iloc[0]
    created = min(row[ANCHOR[etype]] + timedelta(minutes=random.randint(25, 70)), SNAPSHOT)
    add_exception(row["order_id"], etype, created, "Resolved")

exceptions_df = (pd.DataFrame(exceptions)
                 .sort_values("created_ts", ascending=False)
                 .reset_index(drop=True))

# =============================================================================
# 7. Save
# =============================================================================

products_df.to_csv(DATA_DIR / "products.csv", index=False)
inventory_df.to_csv(DATA_DIR / "inventory.csv", index=False)
couriers_df.to_csv(DATA_DIR / "couriers.csv", index=False)
orders_df.to_csv(DATA_DIR / "orders.csv", index=False)
order_items_df.to_csv(DATA_DIR / "order_items.csv", index=False)
exceptions_df.to_csv(DATA_DIR / "exceptions.csv", index=False)
inbound_df.to_csv(DATA_DIR / "inbound_deliveries.csv", index=False)

(DATA_DIR / "meta.json").write_text(json.dumps({
    "snapshot_ts": SNAPSHOT.isoformat(),
    "generated_by": "generate_data.py",
    "seed": SEED,
    "orders_today": N_ORDERS_TODAY,
    "note": ("All event timestamps (stages, labels, exceptions) are <= snapshot_ts. "
             "SLA deadlines and inbound ETAs are future-looking by nature. "
             "The app treats snapshot_ts as 'now'."),
}, indent=2))

# =============================================================================
# 8. Summary + the checks that matter
# =============================================================================

shipped = orders_df[orders_df["status"] == "Shipped"]
met = int((shipped["shipped_ts"] <= shipped["sla_deadline"]).sum())
pending = orders_df[orders_df["status"] != "Shipped"]
breached = int((pending["sla_deadline"] < SNAPSHOT).sum())

print("\nFulfillment Hub - sample data generated")
print("=" * 54)
for name, df in [("products.csv", products_df), ("inventory.csv", inventory_df),
                 ("couriers.csv", couriers_df), ("orders.csv", orders_df),
                 ("order_items.csv", order_items_df), ("exceptions.csv", exceptions_df),
                 ("inbound_deliveries.csv", inbound_df)]:
    print(f"  {name:<24} {len(df):>4} rows")
print("-" * 54)
print(f"  snapshot                {SNAPSHOT:%a %d %b %Y %H:%M}")
print(f"  orders received today   {int((orders_df['received_ts'].dt.date == TODAY).sum())}")
print(f"  backlog from yesterday  {len(orders_df) - int((orders_df['received_ts'].dt.date == TODAY).sum())}")
print(f"  open orders             {len(pending)}   (priority: {int(pending['priority'].sum())})")
print(f"  shipped                 {len(shipped)}   (met SLA: {met}, missed: {len(shipped) - met})")
print(f"  breached (still open)   {breached}")
print(f"  transfer-required SKUs  {TRANSFER_SKUS}")
print(f"  shortage SKUs           {SHORTAGE_SKUS}")
print(f"  count-mismatch SKUs     {mismatch_skus}")
print(f"  orders short in Main    {len(shortage_order_ids)}   (per-line check; the app splits these into blocked vs transfer)")
print(f"  missed pickups          {len(missed_pickup_ids)}")
print(f"  open exceptions         {int((exceptions_df['status'] == 'Open').sum())}")

# --- consistency checks: fail loudly rather than ship a broken dataset -------
assert orders_df["shipped_ts"].max() <= SNAPSHOT, "a stage timestamp is in the future"
for stage in STAGES[1:]:
    col = f"{stage.lower()}_ts"
    prev = STAGES[STAGES.index(stage) - 1].lower() + "_ts"
    both = orders_df[orders_df[col].notna() & orders_df[prev].notna()]
    assert (both[col] >= both[prev]).all(), f"{stage} happens before {prev}"
    assert orders_df[orders_df[col].notna() & orders_df[prev].isna()].empty, \
        f"{stage} timestamp without a {prev} timestamp"

# no exception may be raised before the stage it belongs to
by_id = orders_df.set_index("order_id")
for _, e in exceptions_df.dropna(subset=["order_id"]).iterrows():
    anchor = by_id.loc[e["order_id"], ANCHOR[e["exception_type"]]]
    assert pd.isna(anchor) or e["created_ts"] >= anchor, \
        f"{e['exception_id']} was raised before its stage existed"

# no event timestamp may be after "now" (deadlines and ETAs excluded on purpose)
for col in ["received_ts", "label_created_ts", "picked_ts", "packed_ts",
            "staged_ts", "shipped_ts"]:
    assert orders_df[col].dropna().max() <= SNAPSHOT, f"{col} is after the snapshot"
assert exceptions_df["created_ts"].max() <= SNAPSHOT, "an exception is dated after the snapshot"
assert exceptions_df["resolved_ts"].dropna().max() <= SNAPSHOT, "an exception resolves after the snapshot"

assert 0 < breached < 40, "breach count looks unrealistic - retune the generator"
assert len(TRANSFER_SKUS) == 3 and len(SHORTAGE_SKUS) == 3
print("=" * 54)
print("consistency checks passed\n")
