#!/usr/bin/env python
"""SGR seat watcher -> WhatsApp to one number, run by .github/workflows/sgr.yml.

Also sends a once-a-day "still watching" check-in on the first run after
SGR_HEARTBEAT_HOUR EAT (default 09:00). If the watcher itself stops, the
separate sgr_watchdog.py raises the alarm.

Watches ONE train (default: First Class, Inter-County 08:00, Nairobi Terminus ->
Voi, Sun 20 Dec 2026) and sends a WhatsApp message on a RISING EDGE: the first
run where the class goes from 0 seats to >= MIN_SEATS. It does not book or pay -
it replays the public search the metickets.krc.co.ke form makes
(sgr_availability.py).

State (`last_open`: was the class open at the last LIVE run; `heartbeat_date`) is carried between
runs in SGR_STATE_FILE by the workflow's actions/cache steps. Only a live run
writes it, so a dry or unarmed run can never consume the rising edge.

Env:
  SGR_SCHED / SGR_FROM / SGR_TO / SGR_DATE (mm/dd/yyyy) / SGR_TIME / SGR_DEPART
  SGR_CLASS       "First Class" | "Economy"
  SGR_MIN_SEATS   default 1
  SGR_TO_NUMBER   E.164 recipient
  SGR_HEARTBEAT_HOUR  EAT hour of the daily check-in, default 9
  SGR_ARMED=1     scheduled runs may send (repo variable; unset = log only)
  SGR_TEST=1      send a test message now, whatever the availability
  DRY_RUN=1       compute + print the message, send nothing
"""
import datetime as dt
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sgr_availability as sgr                      # noqa: E402
from wa_send import WhatsAppError, send_number  # noqa: E402

EAT = dt.timezone(dt.timedelta(hours=3))


def _env(name, default=""):
    return (os.environ.get(name) or "").strip() or default


SCHED = _env("SGR_SCHED", "inter_county")
FROM_ID = _env("SGR_FROM", "2")                     # Nairobi Terminus
TO_ID = _env("SGR_TO", "4")                         # Voi
DATE = _env("SGR_DATE", "12/20/2026")               # mm/dd/yyyy, the site's format
TIME = _env("SGR_TIME", "8.00")
DEPART = _env("SGR_DEPART", "08:00 am")             # as the results page prints it
CLASS = _env("SGR_CLASS", "First Class")
MIN_SEATS = int(_env("SGR_MIN_SEATS", "1"))
TO_NUMBER = _env("SGR_TO_NUMBER")                    # repo secret
HEARTBEAT_HOUR = int(_env("SGR_HEARTBEAT_HOUR", "9"))
STATE_FILE = _env("SGR_STATE_FILE", os.path.join(HERE, ".sgr_state.json"))
ARMED = _env("SGR_ARMED") == "1"
TEST = _env("SGR_TEST") == "1"
DRY_RUN = _env("DRY_RUN") == "1"
BOOK_URL = "https://metickets.krc.co.ke/"

TRAIN_RE = re.compile(
    r"Train\s+(\S+)\s+-\s+(.+?)\s+to\s+(.+?)\s+FIRST CLASS\s*-\s*(\d+)\s*SEATS.*?"
    r"ECONOMY\s*-\s*(\d+)\s*SEATS.*?Departure:\s*(\d\d:\d\d\s*[ap]m)", re.I | re.S)


def page_text(html):
    t = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", " ", html, flags=re.I | re.S)
    t = re.sub(r"<[^>]+>", " ", t).replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", t)


def check():
    """-> dict(train, origin, dest, first, economy, depart, fares) for the watched
    departure, or dict(status=...) when it is not on the page."""
    s = sgr.new_session()
    r = s.post(f"{sgr.BASE}/search-view-results.php", data={
        "csrf_token": s.csrf, "schedule_type": SCHED, "terminal_id": FROM_ID,
        "destination_id": TO_ID, "travel-date": DATE, "depature_time": TIME,
    }, headers={"Referer": sgr.BASE + "/"}, timeout=45)
    r.raise_for_status()
    text = page_text(r.text)
    if re.search(r"No Trains Found", text, re.I):
        return {"status": "no-train"}
    for m in TRAIN_RE.finditer(text):
        if m.group(6).lower().replace(" ", "") == DEPART.lower().replace(" ", ""):
            fare = re.search(r"FIRST CLASS\s*-\s*\d+\s*SEATS OPEN Adults KSH\s*([\d,]+)", text, re.I)
            return {"status": "ok", "train": m.group(1), "origin": m.group(2),
                    "dest": m.group(3), "first": int(m.group(4)),
                    "economy": int(m.group(5)), "depart": m.group(6),
                    "first_fare": fare.group(1) if fare else None}
    return {"status": "departure-not-found"}


def seats_of(res):
    return res.get("first" if CLASS.lower().startswith("first") else "economy", 0)


def nice_date():
    return dt.datetime.strptime(DATE, "%m/%d/%Y").strftime("%a %d %b %Y")


def describe(res):
    if res["status"] != "ok":
        return f"{res['status']} (train not listed on the site - sold out or not yet bookable)"
    return f"First Class {res['first']} seats, Economy {res['economy']} seats"


def train_line(res):
    if res.get("status") == "ok":
        return (f"Inter-County {res['depart']} {res['origin']} -> {res['dest']}, "
                f"{nice_date()} (train {res['train']})")
    return f"Inter-County {DEPART} Nairobi -> Voi, {nice_date()}"


def alert_message(res):
    fare = f" (adult KSH {res['first_fare']})" if res.get("first_fare") and CLASS.startswith("First") else ""
    return (f"🚆 SGR {CLASS} seats are OPEN\n"
            f"{train_line(res)}\n"
            f"{CLASS}: {seats_of(res)} seats{fare}\n\n"
            f"Book now: *639# / KRC app / {BOOK_URL}")


def test_message(res):
    return (f"🚆 SGR seat watcher - TEST message\n"
            f"Watching: {CLASS}, {train_line(res)}\n"
            f"Right now: {describe(res)}.\n\n"
            f"From now on: a short check-in here every day around 9am, and a message "
            f"the moment {CLASS} seats open. Checks every ~15 min until departure.")


def heartbeat_message(res, depart_at, now):
    days = (depart_at.date() - now.date()).days
    return (f"🚆 SGR seat watcher - still watching ({days} days to go)\n"
            f"{CLASS}, {train_line(res)}\n"
            f"Right now: {describe(res)}.\n\n"
            f"Next message: the moment {CLASS} seats open, or tomorrow's check-in.")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write("state_updated=1\n")


def send(msg, kind):
    print("--- message ---\n" + msg + "\n---------------")
    if DRY_RUN:
        print(f"DRY RUN - not sent to {TO_NUMBER}")
        return True
    try:
        res = send_number(msg, TO_NUMBER, kind=kind, allow_personal=True)
    except WhatsAppError as e:
        print(f"WhatsApp send FAILED: {e}")
        return False
    print(f"sent to {TO_NUMBER}: {json.dumps(res, default=str)[:300]}")
    return True


def main():
    now = dt.datetime.now(EAT)
    depart_at = dt.datetime.strptime(f"{DATE} {DEPART}", "%m/%d/%Y %I:%M %p").replace(tzinfo=EAT)
    if now > depart_at and not TEST:
        print(f"{now:%Y-%m-%d %H:%M} EAT is past departure {depart_at:%Y-%m-%d %H:%M} - nothing to watch.")
        return 0

    try:
        res = check()
    except Exception as e:                          # site down / layout change
        print(f"check FAILED: {e}")
        return 1
    open_now = res.get("status") == "ok" and seats_of(res) >= MIN_SEATS
    print(f"{now:%Y-%m-%d %H:%M} EAT  {train_line(res)}  ->  {describe(res)}")

    if TEST:
        return 0 if send(test_message(res), "sgr-test") else 1

    state = load_state()
    was_open = bool(state.get("last_open"))
    today = now.date().isoformat()
    beat = (now.hour >= HEARTBEAT_HOUR and state.get("heartbeat_date") != today)
    if open_now and not was_open:
        print(f"RISING EDGE: {CLASS} just opened")
        if not ARMED and not DRY_RUN:
            print("not armed (repo variable SGR_ARMED != 1) - logged only")
            return 0
        if not send(alert_message(res), "sgr"):
            return 1                                # keep state; next run retries
    if beat and (ARMED or DRY_RUN):
        if send(heartbeat_message(res, depart_at, now), "sgr-beat"):
            state["heartbeat_date"] = today
    if ARMED and not DRY_RUN:
        state.update(last_open=open_now, seats=seats_of(res),
                     checked_at=now.isoformat(timespec="minutes"))
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
