#!/usr/bin/env python
"""Watchdog for the SGR seat watcher, run by .github/workflows/sgr_watchdog.yml.

Asks the GitHub API when sgr.yml last finished SUCCESSFULLY (a failed run - site
down, layout change, WhatsApp refusing - counts as not watching). If that is
older than SGR_STALE_HOURS it sends a WhatsApp once per outage, and once more
when the watcher recovers. State (`alerted`) is carried in WATCHDOG_STATE_FILE by
the workflow's actions/cache steps.

It cannot catch GitHub Actions stopping altogether - the daily 09:00 check-in is
the backstop for that: no check-in = not watching.

Env: GITHUB_TOKEN, GITHUB_REPOSITORY, SGR_STALE_HOURS (3), SGR_ALERT_NUMBER,
SGR_DATE / SGR_DEPART (stop after departure), SGR_ARMED=1, DRY_RUN=1.
"""
import datetime as dt
import json
import os
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from wa_send import WhatsAppError, send_number  # noqa: E402

EAT = dt.timezone(dt.timedelta(hours=3))


def _env(name, default=""):
    return (os.environ.get(name) or "").strip() or default


REPO = _env("GITHUB_REPOSITORY")
TOKEN = _env("GITHUB_TOKEN")
STALE_HOURS = float(_env("SGR_STALE_HOURS", "3"))
ALERT_NUMBER = _env("SGR_ALERT_NUMBER")                    # repo secret
DATE = _env("SGR_DATE", "12/20/2026")
DEPART = _env("SGR_DEPART", "08:00 am")
STATE_FILE = _env("WATCHDOG_STATE_FILE", os.path.join(HERE, ".sgr_watchdog.json"))
ARMED = _env("SGR_ARMED") == "1"
DRY_RUN = _env("DRY_RUN") == "1"


def gh(path, **params):
    r = requests.get(f"https://api.github.com/repos/{REPO}/{path}", params=params,
                     headers={"Authorization": f"Bearer {TOKEN}",
                              "Accept": "application/vnd.github+json"}, timeout=30)
    r.raise_for_status()
    return r.json()


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(EAT)


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
    if DRY_RUN or not ARMED:
        print(f"{'DRY RUN' if DRY_RUN else 'not armed'} - not sent to {ALERT_NUMBER}")
        return False
    try:
        send_number(msg, ALERT_NUMBER, kind=kind, allow_personal=True)
    except WhatsAppError as e:
        print(f"WhatsApp send FAILED: {e}")
        return False
    print(f"sent to {ALERT_NUMBER}")
    return True


def main():
    now = dt.datetime.now(EAT)
    depart_at = dt.datetime.strptime(f"{DATE} {DEPART}", "%m/%d/%Y %I:%M %p").replace(tzinfo=EAT)
    if now > depart_at:
        print("past departure - nothing to watch")
        return 0

    ok = gh("actions/workflows/sgr.yml/runs", status="success", per_page=1)["workflow_runs"]
    last = gh("actions/workflows/sgr.yml/runs", per_page=1)["workflow_runs"]
    last_ok = ts(ok[0]["updated_at"]) if ok else None
    hours = (now - last_ok).total_seconds() / 3600 if last_ok else float("inf")
    latest = last[0] if last else None
    print(f"{now:%Y-%m-%d %H:%M} EAT  last successful check: "
          f"{last_ok:%d %b %H:%M} ({hours:.1f} h ago)" if last_ok else "no successful check ever")

    state = load_state()
    stale = hours > STALE_HOURS
    if stale and not state.get("alerted"):
        detail = ""
        if latest:
            detail = (f"\nLatest run: {latest.get('conclusion') or latest.get('status')} at "
                      f"{ts(latest['created_at']):%d %b %H:%M} EAT\n{latest['html_url']}")
        since = f"{last_ok:%a %d %b %H:%M} EAT ({hours:.1f} h ago)" if last_ok else "ever"
        msg = (f"⚠️ SGR seat watcher has STOPPED watching\n"
               f"No successful check since {since}.{detail}\n\n"
               f"No seat alerts will go out until it is fixed.")
        if send(msg, "sgr-watchdog"):
            state["alerted"] = now.isoformat(timespec="minutes")
            save_state(state)
    elif not stale and state.get("alerted"):
        if send(f"✅ SGR seat watcher is watching again (last check {last_ok:%H:%M} EAT).",
                "sgr-watchdog"):
            state.pop("alerted")
            save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
