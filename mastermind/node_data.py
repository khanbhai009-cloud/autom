"""
mastermind/node_data.py — REMOVED (owner's call, 30 Oct 2026).

This was Node 1 "Data Intelligence", which fetched Pinterest analytics
rows from the Analytics_Log / Analytics_logs2 sheet tabs for the CMO.

Why it is gone:
  - Pinterest analytics cannot be fed into this system — there is no
    reliable way to supply those numbers, and the tabs only ever held
    stale, hand-filled data.
  - Direction for the system comes from Pinterest Trends (the trend
    system) + the Sheets style library, not from lagging analytics.
  - The CMO never actually used the fetched rows: its prompt is built
    from the sheet style row, boards and trends. The node only produced
    WorksheetNotFound errors and a misleading "Fallback" flag on every run.

The graph no longer references this module (see mastermind/graph.py).
The state fields a1_raw_analytics / a2_raw_analytics remain in
mastermind/state.py for shape compatibility and stay empty.
"""
