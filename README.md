# 📦 Fulfillment Hub

A single-screen operations dashboard for XYZ Fulfilment: where every order is, what is late,
what to do next, whether the pickers can actually pick it, and which problems are still open.

**Live demo:** _paste your Streamlit link here_

## The four screens
| Tab | Problem it answers |
|---|---|
| Order Tracking | "I can't see the state of an order at a glance" - one ranked *Do this now* list, every row with a reason and a next action |
| Inventory Control | "Stock can't be found / orders can't be packed" - Main vs Backup, bin locations, transfers, physical count overrides system count |
| Exception Management | "Problems are handled informally and forgotten" - every issue has an owner, status, resolution and age |
| Warehouse Floor | A deliberately plain screen for the picking team: pick list, pack/stage/ship buttons, held-back orders |

## Design decisions worth knowing
1. **"Now" is a fixed snapshot** (Tue 06 Oct 2026, 18:00), stored in `data/meta.json`. The app never
   reads the wall clock, so SLA buckets and "hours in stage" are reproducible. In production `snapshot`
   would simply be `datetime.now()`.
2. **Orders are simulated forward through the real flow** (Received → Picked → Packed → Staged → Shipped)
   and truncated at the snapshot, so status and timestamps always agree.
3. **Delays are measured against per-stage targets** (tighter for priority orders), not a flat number.
4. **Ranking is category-first** (breach 1000 > blocked 800 > missed pickup 600 > at risk 400 > stuck 200 >
   transfer 100 > label 50); priority and waiting time only break ties.
5. **A physical count overrides the system quantity** when the two disagree.
6. **User actions are events replayed over the seed data**, never written back to the CSVs.
   "Reset demo" restores the original snapshot.

## Known simplifications (deliberate, and what I'd do next)
- Couriers are assigned at random within the allowed set; the label cutoff is displayed but not enforced,
  and shipped times are not aligned to pickup slots. Next step: choose the courier from label time + cutoff.
- Marking an order picked does not decrement on-hand stock.
- Data is generated sample data (seeded, deterministic), not a live feed.

## Run it
```bash
pip install -r requirements.txt   # needs Python 3.10+; tested on pandas 2.3 and 3.0
python generate_data.py      # builds ./data (deterministic, seed 42)
python test_logic.py         # 41 business-rule checks
python smoke_test.py         # headless UI test: clicks every tab and action
streamlit run app.py
```

## Files
- `generate_data.py` - seeded sample-data generator with built-in consistency assertions
- `logic.py` - all business rules, pure pandas (no Streamlit)
- `app.py` - the Streamlit UI
- `test_logic.py`, `smoke_test.py` - rule tests and UI test
