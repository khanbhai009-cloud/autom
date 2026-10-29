"""
sheets/schedule_state.py — Daily pin plan persistence (Schedule_State tab).

Problem this solves (owner's complaint, 30 Oct 2026): har server restart /
deploy par schedule_daily_pins() purana plan fenk kar NAYA 20-pin train
"abhi se 5-15 min baad" se shuru kar deta tha. Deploy-wali raaton me har
restart ek nayi burst ban jata tha — pins ghanto ki jagah andhadhundh
lagne lagte the.

Ab plan Sheets me rehta hai (key 'daily_plan' → JSON):
  {"day": "2026-10-30", "slots": [offset_min, ...], "created": "..."}
Restart par wahi plan wapas register hota hai — jo slots beet gaye woh
skip, jo bache hain woh apne asli time par. Naya plan sirf naye din
(midnight cron, force=True) par banta hai.
"""
import json
import logging
from sheets.base import _open_worksheet, _throttled_read, _throttled_write

logger = logging.getLogger(__name__)

_KEY = "daily_plan"


def load_daily_plan() -> dict | None:
    """Saved daily plan dict, ya None agar koi plan saved nahi / sheet down."""
    try:
        def _read():
            sheet = _open_worksheet("Schedule_State")
            return sheet.get_all_records()

        for row in _throttled_read(_read):
            if str(row.get("key", "")).strip() == _KEY:
                plan = json.loads(str(row.get("value", "")))
                return plan if isinstance(plan, dict) else None
    except Exception as e:
        logger.warning(f"Schedule_State load failed — {type(e).__name__}: {e}")
    return None


def save_daily_plan(plan: dict) -> None:
    """Plan ko Schedule_State tab me likho (key row update ya append)."""
    payload = json.dumps(plan)

    def _write():
        sheet = _open_worksheet("Schedule_State")
        records = sheet.get_all_records()
        for i, row in enumerate(records):
            if str(row.get("key", "")).strip() == _KEY:
                sheet.update(f"B{i + 2}", [[payload]])
                return
        sheet.append_row([_KEY, payload])

    try:
        _throttled_write(_write)
        logger.info(f"🗓️ Daily plan saved to Sheets (day={plan.get('day')}, slots={len(plan.get('slots', []))})")
    except Exception as e:
        logger.error(f"Schedule_State save failed — {type(e).__name__}: {e}")
