"""
tools/run_control.py — GLOBAL RUN/STOP SWITCH.

Owner ki shikayat (30 Oct 2026): "stop krne ka koi button hi nahi, pura
system loop me chalta rehta hai." Us waqt /api/stop sirf ek state flag
lagata tha jise har scheduled job shuru hote hi reset kar deta tha, aur
chal raha pin cycle use dekhta hi nahi tha — stop asal me kuch rok hi
nahi pata tha.

Ye module EK shared switch hai (threading.Event — FastAPI, APScheduler
aur feeder thread, teeno ek hi process me hain, isliye sab par lagu):

  request_stop()  → scheduler ke scheduled pins skip, generate_pin_image
                    turant mana, ImgBB retry loop beech me abort,
                    vision feeder stop.
  clear_stop()    → sirf explicit resume par: app ka Resume button,
                    manual Run trigger, ya naye din ka midnight plan.
  is_stopped()    → har hot path pehle ye check karta hai.

Restart par switch clear rehta hai (Event memory me hai) — deploy ke
baad system khud chal padega, owner ko kuch nahi karna.
"""
import threading

_stop_event = threading.Event()


def request_stop() -> None:
    _stop_event.set()


def clear_stop() -> None:
    _stop_event.clear()


def is_stopped() -> bool:
    return _stop_event.is_set()
