"""
smoke_test.py
=============
Headless UI test: loads the real app, clicks through every action and fails on
any exception.   python smoke_test.py
"""

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent / "app.py")
FAILS = []


def ok(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILS.append(name)


def clean(at, label):
    errs = [e.value for e in at.exception]
    ok(label, not errs, "; ".join(errs)[:200])
    return not errs


at = AppTest.from_file(APP, default_timeout=90).run()
print("\nInitial render")
clean(at, "app loads with no exception")
ok("4 tabs rendered", len(at.tabs) == 4, f"{len(at.tabs)}")
ok("metrics are on screen", len(at.metric) >= 15, f"{len(at.metric)} metrics")
ok("tables are on screen", len(at.dataframe) >= 8, f"{len(at.dataframe)} tables")


def events():
    return len(at.session_state["events"])


print("\nWarehouse flow")
pick_btns = [b for b in at.button if b.key and b.key.startswith("pick_")]
ok("pick buttons exist", len(pick_btns) > 0, f"{len(pick_btns)}")
oid = pick_btns[0].key.replace("pick_", "")
pick_btns[0].click().run()
clean(at, "clicking Picked works")
ok("Picked recorded an event", events() == 1)

adv = at.selectbox(key="t4_adv")
adv.select(oid).run()
at.button(key="t4_adv_btn").click().run()
clean(at, "Picked -> Packed works")
at.selectbox(key="t4_adv").select(oid).run()
at.button(key="t4_adv_btn").click().run()
clean(at, "Packed -> Staged works")
ok("three events recorded", events() == 3, f"{events()}")

at.selectbox(key="t4_ship").select(oid).run()
at.button(key="t4_ship_btn").click().run()
clean(at, "Staged -> Shipped works")

print("\nInventory transfer")
before = events()
at.button(key="t2_go").click().run()
clean(at, "Record transfer works")
ok("transfer + auto-logged exception recorded", events() == before + 2)
if at.selectbox(key="t2_sku") is not None:
    pass

print("\nExceptions")
at.button(key="t3_save").click().run() if at.button(key="t3_save") else None
clean(at, "updating an exception works")

print("\nOffice queue")
lab = [b for b in at.button if b.key and b.key.startswith("label_")]
if lab:
    lab[0].click().run()
    clean(at, "Create label works")

print("\nReset")
for b in at.sidebar.button:
    if "Reset" in b.label:
        b.click().run()
ok("reset clears the events", events() == 0)
clean(at, "app still healthy after reset")

print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
raise SystemExit(1 if FAILS else 0)
