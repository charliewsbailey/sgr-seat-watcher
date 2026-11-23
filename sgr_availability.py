#!/usr/bin/env python
"""
sgr_availability.py  --  fast SGR (Madaraka Express) seat-availability checker.

This does NOT hack, bypass payment, or auto-buy. It just replays the *same*
public requests that metickets.krc.co.ke's own booking form makes when you
pick a route + date, so you can scan many dates in one shot instead of
clicking through the (slow) web UI. Booking still happens on the website /
app / USSD *639# with M-Pesa -- this only tells you where seats exist.

Usage examples:
  py sgr_availability.py --stations
  py sgr_availability.py --from "Nairobi" --to "Mombasa" --date 09/15/2026
  py sgr_availability.py --from "Nairobi" --to "Mombasa" --days 30
  py sgr_availability.py --from "Mombasa" --to "Nairobi" --days 14 --time 3.00 --type inter_county
"""
import argparse, datetime as dt, re, sys
import requests

BASE = "https://metickets.krc.co.ke"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
OPT_RE = re.compile(r"<option[^>]*value=['\"]([^'\"]*)['\"][^>]*>([^<]*)</option>", re.I)


def new_session():
    """GET the homepage to obtain a PHPSESSID cookie + the CSRF token."""
    s = requests.Session()
    s.headers["User-Agent"] = UA
    r = s.get(BASE + "/", timeout=30)
    r.raise_for_status()
    m = re.search(r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']', r.text, re.I)
    if not m:
        sys.exit("Could not find csrf_token on homepage (site layout changed?)")
    s.csrf = m.group(1)
    return s


def _options(html):
    return [(v.strip(), n.strip()) for v, n in OPT_RE.findall(html) if v.strip()]


def terminals(s, sched):
    r = s.get(f"{BASE}/fetch.php", params={"type": "terminals", "schedule_type": sched},
              headers={"X-Requested-With": "XMLHttpRequest"}, timeout=30)
    return _options(r.text)


def destinations(s, sched, terminal_id):
    r = s.get(f"{BASE}/fetch.php",
              params={"type": "destinations", "schedule_type": sched, "terminal_id": terminal_id},
              headers={"X-Requested-With": "XMLHttpRequest"}, timeout=30)
    return _options(r.text)


def resolve(name, options, what):
    name = name.strip().lower()
    exact = [(v, n) for v, n in options if n.lower() == name]
    part = [(v, n) for v, n in options if name in n.lower()]
    hit = exact or part
    if not hit:
        avail = ", ".join(n for _, n in options)
        sys.exit(f"{what} '{name}' not found. Options: {avail}")
    return hit[0]


def parse_results(html):
    """Pull class/seat/price info out of the results page's visible text."""
    text = re.sub(r"<script\b.*?</script>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    if re.search(r"No Trains Found", text, re.I):
        return {"status": "no-train", "classes": []}
    if re.search(r"[Ff]ully [Bb]ooked", text, re.I):
        # still surface any residual seat lines if present
        pass
    seats = re.findall(r"(ECONOMY|FIRST CLASS|PREMIUM)\s*-\s*([0-9]+)\s*SEATS", text, re.I)
    fares = sorted({int(x.replace(",", "")) for x in re.findall(r"KSH[.: ]*([0-9,]+)", text, re.I)
                    if x.replace(",", "").isdigit() and int(x.replace(",", "")) > 0})
    classes = []
    seen = set()
    for cls, n in seats:
        key = cls.upper()
        if key in seen:
            continue
        seen.add(key)
        classes.append((cls.title(), int(n)))
    status = "available" if classes else ("fully-booked" if re.search(r"fully booked", text, re.I) else "unknown")
    return {"status": status, "classes": classes, "fares": fares}


def search(s, sched, term_id, dest_id, date_mdY, time_val):
    data = {
        "csrf_token": s.csrf,
        "schedule_type": sched,
        "terminal_id": term_id,
        "destination_id": dest_id,
        "travel-date": date_mdY,      # note the HYPHEN, format mm/dd/yyyy
        "depature_time": time_val,    # site's own (mis)spelling
    }
    r = s.post(f"{BASE}/search-view-results.php", data=data,
               headers={"Referer": BASE + "/"}, timeout=40)
    r.raise_for_status()
    return parse_results(r.text)


def premium_search(s, train_type, term_id, dest_id, date_mdY, time_pm):
    """Premium channel = SEPARATE inventory, via the return-form endpoint.
    It uses its OWN station-id namespace (Nairobi=1, Mombasa=2, Athi River=3,
    Emali=4, Kibwezi=5, Mtito Andei=6, Voi=7, Miasenyi=8, Mariakani=9) and a
    '3:00 PM' / '10:00 PM' departure-time format (not '3.00'). Returns the same
    {status, classes, fares} shape as search()."""
    data = {
        "csrf_token": s.csrf,
        "trip_type": "one_way",
        "premium_train_type": train_type,      # 'inter_county' or 'express'
        "premium_terminal_id": term_id,
        "premium_destination_id": dest_id,
        "premium_travel_date": date_mdY,        # mm/dd/yyyy
        "premium_depature_time": time_pm,       # '3:00 PM' / '10:00 PM'
    }
    r = s.post(f"{BASE}/search-view-results-return.php", data=data,
               headers={"Referer": BASE + "/"}, timeout=45)
    r.raise_for_status()
    return parse_results(r.text)


def main():
    ap = argparse.ArgumentParser(description="Fast SGR seat-availability checker")
    ap.add_argument("--type", default="inter_county",
                    choices=["inter_county", "express", "phase2"],
                    help="train service (default inter_county)")
    ap.add_argument("--from", dest="src", help="departure station name (e.g. Nairobi)")
    ap.add_argument("--to", dest="dst", help="destination station name (e.g. Mombasa)")
    ap.add_argument("--time", default="8.00", help="departure time value (inter_county: 8.00 / 2.15 / 3.00)")
    ap.add_argument("--date", help="single date, mm/dd/yyyy")
    ap.add_argument("--days", type=int, help="scan the next N days from today")
    ap.add_argument("--stations", action="store_true", help="just list stations for --type and exit")
    args = ap.parse_args()

    s = new_session()

    if args.stations or not (args.src and args.dst):
        terms = terminals(s, args.type)
        print(f"Stations for '{args.type}':")
        for v, n in terms:
            print(f"  {v:>3}  {n}")
        if args.stations:
            return
        sys.exit("\nProvide --from and --to (see names above).")

    terms = terminals(s, args.type)
    tid, tname = resolve(args.src, terms, "Departure")
    dests = destinations(s, args.type, tid)
    did, dname = resolve(args.dst, dests, "Destination")

    if args.date:
        dates = [args.date]
    else:
        n = args.days or 14
        today = dt.date.today()
        dates = [(today + dt.timedelta(days=i)).strftime("%m/%d/%Y") for i in range(n)]

    print(f"\n{tname} -> {dname}  |  {args.type}  |  dep {args.time}\n" + "-" * 64)
    for d in dates:
        try:
            res = search(s, args.type, tid, did, d, args.time)
        except Exception as e:
            print(f"{d}  ERROR: {e}")
            continue
        if res["status"] == "available":
            seats = "  ".join(f"{c}:{n}" for c, n in res["classes"])
            fares = ("  fares KSH " + "/".join(str(f) for f in res["fares"])) if res.get("fares") else ""
            print(f"{d}  AVAILABLE   {seats}{fares}")
        elif res["status"] == "fully-booked":
            print(f"{d}  FULLY BOOKED")
        elif res["status"] == "no-train":
            print(f"{d}  no train / not yet on sale")
        else:
            print(f"{d}  (unknown / no seat data)")


if __name__ == "__main__":
    main()
