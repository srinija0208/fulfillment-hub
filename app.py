"""
Fulfillment Hub - the application
=================================
A single screen for XYZ's fulfilment day. Everything is read from the CSV
snapshot in ./data (see generate_data.py) and every rule lives in logic.py.

    streamlit run app.py

Tabs
  1. Order Tracking        where is every order, what is late, what to do next
  2. Inventory Control     can the pickers actually pick it (Main vs Backup, bins, counts)
  3. Exception Management  the problems that used to be handled informally and forgotten
  4. Warehouse Floor       a deliberately plain screen for the picking team
"""

import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

import logic

st.set_page_config(page_title="Fulfillment Hub - XYZ", page_icon="📦", layout="wide")

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

# If the CSVs were not committed alongside the app, build them once on first run.
if not (DATA_DIR / "orders.csv").exists():
    subprocess.run([sys.executable, str(ROOT / "generate_data.py")], check=True, cwd=ROOT)


# ----------------------------------------------------------------------------
# Data + session state
# ----------------------------------------------------------------------------
# The CSVs are the snapshot; anything the user does here is appended to an event
# list and replayed. Nothing is silently written back to disk.

@st.cache_data(show_spinner=False)
def get_data() -> dict:
    return logic.load_data(DATA_DIR)


if "events" not in st.session_state:
    st.session_state.events = []


def do(event: dict) -> None:
    """Record an action and re-render."""
    st.session_state.events.append(event)
    st.rerun()


data = get_data()
SNAP = data["snapshot"]
ITEMS = data["items"]
COURIERS = data["couriers"]
PRODUCTS = data["products"]
INBOUND = data["inbound"]

orders_raw, inventory_raw, exceptions_raw = logic.apply_events(
    data["orders"], data["inventory"], data["exceptions"], st.session_state.events
)
o = logic.derive_orders(orders_raw, ITEMS, COURIERS, SNAP)
sku_check, inventory = logic.stock_check(o, ITEMS, inventory_raw)
blocked = logic.blocked_orders(o, ITEMS, sku_check)
transfer = logic.transfer_needed_orders(o, ITEMS, sku_check)
actions = logic.attention_list(o, blocked, transfer)

today = SNAP.date()
received_today = int((data["orders"]["received_ts"].dt.date == today).sum())
open_orders = o[o["status"] != "Shipped"]
open_priority = int(open_orders["priority"].sum())
value_at_risk = int(o.loc[o["sla_status"].isin(["Breached", "At Risk"]), "order_value"].sum())


# ----------------------------------------------------------------------------
# Styling helpers
# ----------------------------------------------------------------------------

SLA_COLOURS = {
    "Breached": "background-color:#fde0e0;color:#8b1a1a;font-weight:600",
    "At Risk": "background-color:#fff3cd;color:#7a5b00;font-weight:600",
    "On Track": "background-color:#e7f4e8;color:#1b5e20",
    "Met": "background-color:#e7f4e8;color:#1b5e20",
    "Missed": "background-color:#fde0e0;color:#8b1a1a",
    "Stock Shortage": "background-color:#fde0e0;color:#8b1a1a;font-weight:600",
    "Transfer Required": "background-color:#fff3cd;color:#7a5b00;font-weight:600",
    "OK": "background-color:#e7f4e8;color:#1b5e20",
    "Open": "background-color:#fde0e0;color:#8b1a1a;font-weight:600",
    "In Progress": "background-color:#fff3cd;color:#7a5b00",
    "Resolved": "background-color:#e7f4e8;color:#1b5e20",
}


def styled(df: pd.DataFrame, cols: list):
    cols = [c for c in cols if c in df.columns]
    return df.style.map(lambda v: SLA_COLOURS.get(v, ""), subset=cols)


def badge(priority: bool) -> str:
    return "🔴 Priority" if priority else "-"


# ----------------------------------------------------------------------------
# Header
# ----------------------------------------------------------------------------

st.title("📦 Fulfillment Hub")
st.caption(
    f"Snapshot **{SNAP:%a %d %b %Y, %H:%M}** - the app scores every order against that "
    "moment, so these numbers are reproducible. Sample data for XYZ Fulfilment."
)

with st.sidebar:
    st.subheader("The day at a glance")
    st.metric("Orders today", received_today)
    st.metric("Open orders", len(open_orders), help="Not yet shipped")
    st.metric("Priority open", open_priority)
    st.metric("Value at risk", f"₹{value_at_risk:,}",
              help="Breached, or at risk of missing SLA")
    st.divider()
    st.caption(
        "**How to read this.** Every order carries a status, a timestamp per stage and an "
        "SLA clock. The rules (delay targets, risk windows, stock logic) live in `logic.py`; "
        "the data comes from `generate_data.py`."
    )
    st.divider()
    if st.button("Reset demo (undo my actions)", width="stretch"):
        st.session_state.events = []
        st.rerun()
    if st.session_state.events:
        st.caption(f"Actions recorded this session: **{len(st.session_state.events)}**")

tab1, tab2, tab3, tab4 = st.tabs(
    ["📊 Order Tracking", "🏭 Inventory Control", "⚠️ Exception Management", "🧑‍🏭 Warehouse Floor"]
)


# ============================================================================
# TAB 1 - ORDER TRACKING
# ============================================================================
with tab1:
    st.caption(
        "Answers the first problem in the brief: *you cannot see the state of an order at a "
        "glance*. Status, delay, SLA risk and the next action all live in one ranked list."
    )

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Orders received today", received_today)
    k2.metric("Still open", len(open_orders))
    k3.metric("Priority open", open_priority)
    k4.metric("🔴 SLA breached", int((o["sla_status"] == "Breached").sum()))
    k5.metric("🟡 At risk", int((o["sla_status"] == "At Risk").sum()))
    k6.metric("Delayed in stage", int(o["is_stuck"].sum()),
              help="Waiting longer than the target time for its current stage")

    st.subheader("Do this now")
    st.caption("Every problem in one list - most urgent first, with the reason and the next action.")
    if actions.empty:
        st.success("Nothing needs chasing right now.")
    else:
        show = actions.head(logic.TOP_N_ACTIONS).copy()
        show["priority"] = show["priority"].map(badge)
        st.dataframe(
            show.rename(columns={
                "order_id": "Order", "priority": "Priority", "channel": "Channel",
                "city": "City", "status": "Stage", "sla_status": "SLA",
                "hours_in_stage": "Hrs in stage", "order_value": "Value (₹)",
                "courier_name": "Courier", "reason": "Why it is here",
                "next_action": "Next action"}),
            width="stretch", hide_index=True,
            column_config={
                "Value (₹)": st.column_config.NumberColumn(format="₹%d"),
                "Hrs in stage": st.column_config.NumberColumn(format="%.1f h"),
            },
        )
        st.caption(
            f"Showing the {len(show)} most urgent of **{len(actions)}** flagged orders. "
            "Ranking is severity first (breach > blocked > missed pickup > at risk > delay), "
            "then priority, then time already waited."
        )

    st.divider()
    c_left, c_right = st.columns(2)

    with c_left:
        st.subheader("Where the day's orders are")
        funnel = (o["status"].value_counts().reindex(logic.STAGES).fillna(0)
                  .astype(int).rename("Orders"))
        # numbered labels keep the funnel in flow order (charts sort labels A-Z)
        funnel.index = [f"{i + 1}. {s}" for i, s in enumerate(funnel.index)]
        st.bar_chart(funnel, height=280)
        st.caption("Received → Picked → Packed → Staged → Shipped. Anything piling up "
                   "early in the funnel is tomorrow's complaint.")

    with c_right:
        st.subheader("Are we keeping our promises?")
        sla = (o["sla_status"].value_counts()
               .reindex(["On Track", "At Risk", "Breached", "Met", "Missed"]).fillna(0)
               .astype(int).rename("Orders"))
        sla.index = [f"{i + 1}. {s}" for i, s in enumerate(sla.index)]
        st.bar_chart(sla, height=280)
        st.caption("Breached / Missed = the promise is already broken. At Risk = still savable "
                   "today. Priority orders have a same-day promise, regular orders 24 hours.")

    st.divider()
    st.subheader("Office queue - labels to create")
    st.caption("The office team reviews the order and prints the shipping label. "
               "These orders are still waiting for theirs.")
    waiting_label = o[o["awaiting_label"]].sort_values(["priority", "received_ts"],
                                                       ascending=[False, True])
    if waiting_label.empty:
        st.success("Every order has a label.")
    else:
        head = waiting_label.head(5)
        cols = st.columns(len(head))
        for col, (_, row) in zip(cols, head.iterrows()):
            with col:
                tag = "🔴 Priority" if row["priority"] else "Regular"
                st.markdown(f"**{row['order_id']}**  \n{tag}  \n{row['courier_name']}")
                st.caption(f"waiting {row['hours_in_stage']:.0f}h")
                if st.button("Create label", key=f"label_{row['order_id']}",
                             width="stretch"):
                    do({"kind": "label", "order_id": row["order_id"], "ts": SNAP})
        if len(waiting_label) > 5:
            st.caption(f"...and {len(waiting_label) - 5} more.")

    st.divider()
    st.subheader("All orders")
    f1, f2, f3, f4 = st.columns([2, 1, 1, 1])
    status_f = f1.multiselect("Stage", logic.STAGES, default=logic.STAGES, key="t1_stage")
    priority_f = f2.selectbox("Priority", ["All", "Priority only", "Regular only"], key="t1_pri")
    sla_f = f3.multiselect("SLA", ["On Track", "At Risk", "Breached", "Met", "Missed"],
                           default=["On Track", "At Risk", "Breached", "Met", "Missed"],
                           key="t1_sla")
    search = f4.text_input("Search order / customer", key="t1_search")

    view = o[o["status"].isin(status_f) & o["sla_status"].isin(sla_f)]
    if priority_f == "Priority only":
        view = view[view["priority"]]
    elif priority_f == "Regular only":
        view = view[~view["priority"]]
    if search:
        view = view[view["order_id"].str.contains(search, case=False, regex=False)
                    | view["customer_name"].str.contains(search, case=False, regex=False)]

    table = view.sort_values(["priority", "hours_in_stage"], ascending=[False, False]).copy()
    table["priority"] = table["priority"].map(badge)
    st.dataframe(
        styled(
            table[["order_id", "priority", "channel", "customer_name", "city", "status",
                   "sla_status", "hours_in_stage", "order_value", "courier_name",
                   "sla_deadline"]].rename(columns={
                       "order_id": "Order", "priority": "Priority", "channel": "Channel",
                       "customer_name": "Customer", "city": "City", "status": "Stage",
                       "sla_status": "SLA", "hours_in_stage": "Hrs in stage",
                       "order_value": "Value (₹)", "courier_name": "Courier",
                       "sla_deadline": "SLA deadline"}),
            ["SLA"]),
        width="stretch", hide_index=True,
        column_config={
            "Value (₹)": st.column_config.NumberColumn(format="₹%d"),
            "Hrs in stage": st.column_config.NumberColumn(format="%.1f h"),
            "SLA deadline": st.column_config.DatetimeColumn(format="ddd DD MMM, HH:mm"),
        },
    )
    st.caption(f"{len(table)} of {len(o)} orders shown.")

    st.divider()
    st.subheader("Open one order (the full trail)")
    sc1, sc2 = st.columns([1, 2])
    query = sc1.text_input("Find an order", value="", key="t1_find",
                           placeholder="e.g. ORD0010, or a customer name")
    candidates = o.sort_values("received_ts", ascending=False)["order_id"].tolist()
    if query:
        hit = (o["order_id"].str.contains(query, case=False, regex=False)
               | o["customer_name"].str.contains(query, case=False, regex=False))
        candidates = o.loc[hit, "order_id"].tolist()
    picked = sc2.selectbox("Order", candidates[:100], key="t1_pick") if candidates else None

    if picked:
        row = o.loc[o["order_id"] == picked].iloc[0]
        d1, d2 = st.columns([2, 3])
        with d1:
            st.markdown(f"### {row['order_id']}" + ("  ·  🔴 Priority" if row["priority"] else ""))
            st.markdown(
                f"**{row['customer_name']}** · {row['city']}  \n"
                f"Channel: **{row['channel']}** · Value: **₹{row['order_value']:,}**  \n"
                f"Stage: **{row['status']}** · SLA: **{row['sla_status']}** "
                f"({row['hours_to_deadline']:+.1f}h)  \n"
                f"Courier: **{row['courier_name']}** · pickup slot "
                f"**{row['courier_pickup_ts']:%H:%M}**  \n"
                f"Ships from: **{row['warehouse']}** warehouse"
            )
            if row["pickup_missed"]:
                st.error("The courier's pickup slot has passed and this box is still in staging.")
            if row["order_id"] in blocked:
                st.error("Blocked: required stock is not available in Main or Backup.")
            elif row["order_id"] in transfer:
                st.warning("Needs a stock transfer from Backup before it can be picked.")

        with d2:
            trail, previous = [], None
            for stage in logic.STAGES:
                ts = orders_raw.loc[orders_raw["order_id"] == picked,
                                    logic.STAGE_TS_COL[stage]].iloc[0]
                has_ts = pd.notna(ts)
                trail.append({
                    "Stage": stage,
                    "Timestamp": ts if has_ts else None,
                    "Waiting (h)": round((SNAP - ts).total_seconds() / 3600, 1) if has_ts else None,
                    "Gap from previous (h)": round((ts - previous).total_seconds() / 3600, 1)
                    if (has_ts and previous is not None) else None,
                })
                if has_ts:
                    previous = ts
            st.dataframe(pd.DataFrame(trail), width="stretch", hide_index=True,
                         column_config={
                             "Timestamp": st.column_config.DatetimeColumn(
                                 format="ddd DD MMM, HH:mm"),
                             "Waiting (h)": st.column_config.NumberColumn(format="%.1f"),
                             "Gap from previous (h)": st.column_config.NumberColumn(format="%.1f"),
                         })

            lines = ITEMS[ITEMS["order_id"] == picked].copy()
            bins = sku_check.set_index("product_id")["main_bin"].to_dict()
            lines["Bin"] = lines["product_id"].map(bins).fillna("-")
            lines["Line value"] = lines["quantity"] * lines["unit_price"]
            st.dataframe(
                lines[["product_id", "product_name", "variant", "quantity", "Bin", "Line value"]]
                .rename(columns={"product_id": "SKU", "product_name": "Product",
                                 "variant": "Variant", "quantity": "Qty",
                                 "Line value": "Value (₹)"}),
                width="stretch", hide_index=True,
                column_config={"Value (₹)": st.column_config.NumberColumn(format="₹%d")},
            )


# ============================================================================
# TAB 2 - INVENTORY CONTROL
# ============================================================================
with tab2:
    st.subheader("Inventory Control")
    st.caption(
        "Second problem: *stock in the spreadsheet cannot be found, or has gone missing, so "
        "orders cannot be packed*. This checks Main against the orders still waiting to be "
        "picked, says what to move from Backup - and trusts a physical count over the system "
        "when the two disagree."
    )

    below_reorder = inventory[(inventory["warehouse"] == "Main")
                              & (inventory["quantity"] < inventory["reorder_level"])]
    mismatches = inventory[inventory["count_variance"].fillna(0) != 0]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🔄 Transfer required", int((sku_check["status"] == "Transfer Required").sum()),
              help="Main is short, Backup can cover it")
    m2.metric("🔴 Stock shortage", int((sku_check["status"] == "Stock Shortage").sum()),
              help="Neither warehouse can cover the orders waiting")
    m3.metric("Below reorder level", len(below_reorder))
    m4.metric("Count mismatches", len(mismatches), help="Physical count is not the system quantity")

    st.subheader("What the pickers need")
    st.caption("Only SKUs required by orders still waiting to be picked, most blocking first, "
               "with bin locations so nobody hunts the aisle.")
    if sku_check.empty:
        st.info("No orders are waiting to be picked.")
    else:
        display = sku_check.merge(PRODUCTS, on="product_id", how="left")
        st.dataframe(
            styled(display[["product_id", "product_name", "variant", "required_qty",
                            "main_available", "backup_available", "transfer_qty",
                            "main_bin", "backup_bin", "status"]]
                   .rename(columns={
                       "product_id": "SKU", "product_name": "Product", "variant": "Variant",
                       "required_qty": "Needed (unpicked orders)", "main_available": "In Main",
                       "backup_available": "In Backup", "transfer_qty": "Transfer qty",
                       "main_bin": "Main bin", "backup_bin": "Backup bin", "status": "Status"}),
                   ["Status"]),
            width="stretch", hide_index=True,
        )

    st.subheader("Move stock Backup → Main")
    st.caption("Orders only ship from Main, so a shortfall here stops a picker. "
               "Recording a transfer updates both warehouses immediately.")
    moves = sku_check[sku_check["status"] == "Transfer Required"]
    if moves.empty:
        st.success("No transfers needed right now.")
    else:
        t1, t2, t3 = st.columns([2, 1, 1])
        options = moves["product_id"].tolist()
        chosen = t1.selectbox("SKU to transfer", options, key="t2_sku")
        sel = moves.set_index("product_id").loc[chosen]
        # keyed per SKU so the default and the cap reset when another SKU is chosen
        qty = t2.number_input("Units", min_value=1, max_value=int(sel["backup_available"]),
                              value=int(sel["transfer_qty"]), key=f"t2_qty_{chosen}")
        t3.write("")
        t3.write("")
        if t3.button("Record transfer", width="stretch", key="t2_go"):
            st.session_state.events.append(
                {"kind": "transfer", "product_id": chosen, "qty": int(qty)})
            st.session_state.events.append({
                "kind": "exception",
                "row": {
                    "exception_id": logic.next_exception_id(exceptions_raw),
                    "order_id": None, "product_id": chosen,
                    "exception_type": "Inventory Shortage", "severity": "High",
                    "owner": "Warehouse Team", "stage": "Inventory",
                    "description": f"{int(qty)} units moved from Backup to Main for picking.",
                    "status": "Resolved", "created_ts": SNAP, "resolved_ts": SNAP,
                    "resolution": "Stock transferred from Backup warehouse.",
                },
            })
            st.rerun()

    st.divider()
    st.subheader("Full stock view")
    inv_side = inventory.pivot(index="product_id", columns="warehouse",
                               values=["quantity", "bin_location"])
    flat = pd.DataFrame({
        "In Main (system)": inv_side[("quantity", "Main")],
        "In Backup": inv_side[("quantity", "Backup")],
        "Counted (Main)": inventory[inventory["warehouse"] == "Main"]
        .set_index("product_id")["counted_qty"],
        "Main bin": inv_side[("bin_location", "Main")],
        "Backup bin": inv_side[("bin_location", "Backup")],
    }).reset_index().merge(PRODUCTS[["product_id", "product_name", "variant"]],
                           on="product_id", how="left")
    flat["Counted (Main)"] = pd.to_numeric(flat["Counted (Main)"], errors="coerce")
    flat["In Main (system)"] = pd.to_numeric(flat["In Main (system)"], errors="coerce").fillna(0)
    flat["In Backup"] = pd.to_numeric(flat["In Backup"], errors="coerce").fillna(0)
    flat["Effective Main"] = flat["Counted (Main)"].fillna(flat["In Main (system)"])
    flat["Variance"] = flat["Counted (Main)"] - flat["In Main (system)"]
    flat["Flag"] = "OK"
    flat.loc[flat["Variance"].fillna(0) != 0, "Flag"] = "Count mismatch - verify"
    flat.loc[flat["Effective Main"] == 0, "Flag"] = "Nothing in Main"

    q2 = st.text_input("Filter by SKU or product", key="t2_search")
    if q2:
        flat = flat[flat["product_id"].str.contains(q2, case=False, regex=False)
                    | flat["product_name"].str.contains(q2, case=False, regex=False)]
    st.dataframe(
        flat[["product_id", "product_name", "variant", "In Main (system)", "In Backup",
              "Counted (Main)", "Effective Main", "Variance", "Main bin", "Backup bin", "Flag"]]
        .rename(columns={"product_id": "SKU", "product_name": "Product", "variant": "Variant"}),
        width="stretch", hide_index=True,
    )
    st.caption("Where a physical count exists, **Effective Main** uses the count, because the "
               "shelf is the truth. Variance shows how far the spreadsheet has drifted.")

    with st.expander("📥 Inbound deliveries - unload, check, put away"):
        st.caption("The last step of the loop: arriving stock has to be checked and shelved "
                   "before it can be sold. Includes what is coming that clears a shortage.")
        ib = INBOUND.merge(PRODUCTS[["product_id", "product_name", "variant"]],
                           on="product_id", how="left")
        ib["Needed by open orders"] = ib["product_id"].map(
            sku_check.set_index("product_id")["required_qty"]).fillna(0).astype(int)
        st.dataframe(
            ib[["inbound_id", "supplier", "product_id", "product_name", "variant", "quantity",
                "eta_ts", "status", "Needed by open orders"]]
            .rename(columns={"inbound_id": "Delivery", "product_id": "SKU",
                             "product_name": "Product", "variant": "Variant",
                             "quantity": "Qty", "eta_ts": "ETA", "status": "Status"}),
            width="stretch", hide_index=True,
            column_config={"ETA": st.column_config.DatetimeColumn(format="ddd DD MMM, HH:mm")},
        )


# ============================================================================
# TAB 3 - EXCEPTION MANAGEMENT
# ============================================================================
with tab3:
    st.caption(
        "Third problem: *problems are handled informally and are easy to forget*. Every "
        "shortage, wrong variant, misplaced box and missed pickup is logged with an owner, a "
        "status and a resolution - so patterns surface instead of repeating."
    )
    metrics = logic.issue_metrics(exceptions_raw, SNAP)

    e1, e2, e3, e4, e5, e6 = st.columns(6)
    e1.metric("Open", metrics["open"])
    e2.metric("In progress", metrics["in_progress"])
    e3.metric("Resolved", metrics["resolved"])
    e4.metric("🔴 Unresolved, high severity", metrics["open_high"])
    e5.metric("Avg. time to resolve", f"{metrics['avg_resolve_hours']:.1f} h")
    e6.metric("Oldest unresolved exception", f"{metrics['oldest_open_hours']:.1f} h")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("What goes wrong, and how much is still open")
        pivot = (exceptions_raw.pivot_table(index="exception_type", columns="status",
                                            values="exception_id", aggfunc="count").fillna(0))
        st.bar_chart(pivot, height=300)
    with right:
        st.subheader("What this is telling us")
        top = exceptions_raw["exception_type"].value_counts()
        stuck_open = exceptions_raw[(exceptions_raw["status"] != "Resolved")
                                    & (exceptions_raw["created_ts"]
                                       < SNAP - pd.Timedelta(hours=12))]
        st.markdown(
            f"- **{top.index[0]}** is the most frequent exception ({int(top.iloc[0])} logged) - "
            "worth fixing at the source, not the symptom.\n"
            f"- **{len(stuck_open)}** exception(s) have been unresolved for more than 12 hours: exactly the "
            "case where things get forgotten.\n"
            f"- **{metrics['open_high']}** open unresolved exception(s) are high severity and affect a "
            "customer's delivery date.\n"
            f"- Average resolution time is **{metrics['avg_resolve_hours']:.1f} h** - "
            "use it as the baseline to beat."
        )

    st.divider()
    st.subheader("Exception log")
    g1, g2, g3 = st.columns(3)
    ex_status_f = g1.multiselect("Status", sorted(exceptions_raw["status"].unique()),
                                 default=sorted(exceptions_raw["status"].unique()),
                                 key="t3_status")
    ex_sev_f = g2.multiselect("Severity", sorted(exceptions_raw["severity"].unique()),
                              default=sorted(exceptions_raw["severity"].unique()), key="t3_sev")
    ex_owner_f = g3.multiselect("Owner", sorted(exceptions_raw["owner"].unique()),
                                default=sorted(exceptions_raw["owner"].unique()), key="t3_owner")

    ex_view = exceptions_raw[
        exceptions_raw["status"].isin(ex_status_f)
        & exceptions_raw["severity"].isin(ex_sev_f)
        & exceptions_raw["owner"].isin(ex_owner_f)
    ].sort_values("created_ts", ascending=False)

    st.dataframe(
        styled(
            ex_view[["exception_id", "order_id", "product_id", "exception_type", "severity",
                     "stage", "owner", "status", "created_ts", "resolved_ts", "resolution"]]
            .rename(columns={"exception_id": "Exception", "order_id": "Order", "product_id": "SKU",
                             "exception_type": "Type", "severity": "Severity",
                             "stage": "Stage it happened", "owner": "Owner",
                             "status": "Status", "created_ts": "Raised",
                             "resolved_ts": "Resolved at", "resolution": "Resolution"}),
            ["Status"]),
        width="stretch", hide_index=True,
        column_config={
            "Raised": st.column_config.DatetimeColumn(format="ddd DD MMM, HH:mm"),
            "Resolved at": st.column_config.DatetimeColumn(format="ddd DD MMM, HH:mm"),
        },
    )

    a1, a2 = st.columns(2)
    with a1:
        with st.expander("➕ Log a new exception", expanded=False):
            with st.form("new_exception", clear_on_submit=True):
                order_id = st.text_input("Order ID", placeholder="ORD0123 (blank if not order-specific)")
                sku = st.text_input("SKU", placeholder="optional")
                etype = st.selectbox("Exception Type", logic.EXCEPTION_TYPES)
                sev = st.selectbox("Severity", ["High", "Medium", "Low"])
                owner = st.selectbox("Owner", logic.EXCEPTION_OWNERS)
                where = st.selectbox("Where it happened",
                                     ["Picking", "Packing", "Staging", "Inventory", "Dispatch"])
                what = st.text_area("What happened?", placeholder="Optional note")
                if st.form_submit_button("Log exception"):
                    oid_in = order_id.strip().upper()
                    sku_in = sku.strip().upper()
                    if oid_in and oid_in not in set(o["order_id"]):
                        st.error(f"Order {oid_in} does not exist.")
                    elif sku_in and sku_in not in set(PRODUCTS["product_id"]):
                        st.error(f"SKU {sku_in} does not exist.")
                    else:
                        st.session_state.events.append({
                            "kind": "exception",
                            "row": {
                                "exception_id": logic.next_exception_id(exceptions_raw),
                                "order_id": oid_in or None, "product_id": sku_in or None,
                                "exception_type": etype, "severity": sev, "owner": owner,
                                "stage": where,
                                "description": what or "Logged from the dashboard.",
                                "status": "Open", "created_ts": SNAP, "resolved_ts": None,
                                "resolution": "No action yet.",
                            },
                        })
                        st.rerun()
    with a2:
        with st.expander("✅ Update an exception", expanded=False):
            openish = exceptions_raw[exceptions_raw["status"] != "Resolved"]
            if openish.empty:
                st.success("No open exceptions.")
            else:
                issue_id = st.selectbox("Exception", openish["exception_id"].tolist(), key="t3_pick")
                new_status = st.selectbox("Set status", ["In Progress", "Resolved"], key="t3_new")
                res_note = st.text_input("Resolution note", value="Resolved after review.",
                                         key="t3_note")
                if st.button("Save", key="t3_save"):
                    do({"kind": "resolve", "exception_id": issue_id, "ts": SNAP,
                        "resolution": res_note, "status": new_status})


# ============================================================================
# TAB 4 - WAREHOUSE FLOOR
# ============================================================================
with tab4:
    st.caption(
        "For the picking team: no filters, no tables, one instruction per box. Priority orders "
        "are at the top in red. Orders whose stock is not on the shelf are kept out of the way "
        "so nobody walks the aisle for stock that is not there."
    )

    todo = logic.pick_list(o, ITEMS, sku_check, blocked | transfer, limit=12)
    st.subheader(f"Pick list - {len(todo)} order(s), priority first")
    if not todo:
        st.success("Nothing waiting to be picked.")
    for order in todo:
        with st.container(border=True):
            head, button = st.columns([5, 1])
            with head:
                flag = "🔴 **PRIORITY - ship today**" if order["priority"] else "Regular"
                st.markdown(f"### {order['order_id']} · {flag}")
                st.markdown(f"{order['channel']} · {order['courier_name']} · "
                            f"waiting **{order['minutes_waiting']} min**")
            with button:
                st.write("")
                if st.button("✅ Picked", key=f"pick_{order['order_id']}",
                             width="stretch"):
                    do({"kind": "advance", "order_id": order["order_id"], "to": "Picked",
                        "ts": SNAP})
            for pick in order["picks"]:
                st.markdown(f"&nbsp;&nbsp;&nbsp;`{pick['qty']} x` **{pick['name']}** "
                            f"({pick['variant']}) &nbsp;→&nbsp; shelf **{pick['bin']}**",
                            unsafe_allow_html=True)

    if blocked:
        st.subheader("⛔ Do not pick yet - stock is not on the shelf")
        st.caption("Held back deliberately: neither warehouse has enough. "
                   "The inbound list on the Inventory tab shows what is on its way.")
        rows = o[o["order_id"].isin(blocked)][["order_id", "priority", "status",
                                               "hours_in_stage", "courier_name"]].copy()
        rows["priority"] = rows["priority"].map(badge)
        st.dataframe(rows.rename(columns={"order_id": "Order", "priority": "Priority",
                                          "status": "Stage", "hours_in_stage": "Hrs waiting",
                                          "courier_name": "Courier"}),
                     width="stretch", hide_index=True)

    transfer_only = transfer - blocked
    if transfer_only:
        st.subheader("🔄 Waiting for a stock transfer")
        st.caption("The stock is in Backup. Record the transfer on the Inventory Control tab "
                   "and these orders move into the pick list.")
        held = o[o["order_id"].isin(transfer_only)][["order_id", "priority", "hours_in_stage",
                                                "courier_name"]].copy()
        held["priority"] = held["priority"].map(badge)
        st.dataframe(held.rename(columns={"order_id": "Order", "priority": "Priority",
                                          "hours_in_stage": "Hrs waiting",
                                          "courier_name": "Courier"}),
                     width="stretch", hide_index=True)

    st.divider()
    st.subheader("Staging - boxes waiting for the courier")
    board = logic.staging_board(o, SNAP)
    if board.empty:
        st.info("Nothing in staging.")
    else:
        b = board[["order_id", "priority", "courier_name", "pickup_slot", "slot_status",
                   "hours_in_stage"]].copy()
        b["priority"] = b["priority"].map(badge)
        st.dataframe(
            styled(b.rename(columns={"order_id": "Order", "priority": "Priority",
                                     "courier_name": "Courier", "pickup_slot": "Pickup",
                                     "slot_status": "Slot", "hours_in_stage": "Hrs in staging"}),
                   ["Slot"]),
            width="stretch", hide_index=True,
            column_config={"Hrs in staging": st.column_config.NumberColumn(format="%.1f h")})
        st.caption("If a slot has gone and the box is still here, the courier owes a pickup - "
                   "tell the office team so it is not forgotten.")
        with st.expander("Hand a box to the courier"):
            choice = st.selectbox("Order staged and collected",
                                  board["order_id"].tolist(), key="t4_ship")
            if st.button("Mark as shipped", key="t4_ship_btn"):
                do({"kind": "advance", "order_id": choice, "to": "Shipped", "ts": SNAP})

    with st.expander("📦 Move a box along (pack / stage)"):
        mid = o[o["status"].isin(["Picked", "Packed"])].sort_values("received_ts")
        if mid.empty:
            st.caption("Nothing waiting to be packed or staged.")
        else:
            oid = st.selectbox("Order", mid["order_id"].tolist(), key="t4_adv")
            nxt = logic.NEXT_STAGE[mid.loc[mid["order_id"] == oid, "status"].iloc[0]]
            if st.button(f"Mark as {nxt}", key="t4_adv_btn"):
                do({"kind": "advance", "order_id": oid, "to": nxt, "ts": SNAP})

    with st.expander("🚚 Courier options and today's slots"):
        st.dataframe(COURIERS[["courier_name", "service", "cost_per_shipment", "speed_days",
                               "label_cutoff", "pickup_time", "note"]]
                     .rename(columns={"courier_name": "Courier", "service": "Service",
                                      "cost_per_shipment": "Cost (₹)", "speed_days": "Days",
                                      "label_cutoff": "Label cutoff", "pickup_time": "Pickup",
                                      "note": "Note"}),
                     width="stretch", hide_index=True)
        st.caption("The label cutoff matters as much as the price: an order that misses it waits "
                   "a full day. Priority orders are booked with the express couriers only.")