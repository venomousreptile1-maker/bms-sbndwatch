#!/usr/bin/env python3
"""
bms_venue_watch.py - Watch a movie that's ALREADY on sale and alert when
new theatres (or new dates, or extra shows) come online.

URL patterns (confirmed July 2026):
  showtimes  https://in.bookmyshow.com/movies/{city}/{slug}/buytickets/{EVENT}/{YYYYMMDD}
  seat map   https://in.bookmyshow.com/movies/{citycode}/seat-layout/{EVENT}/{VENUE}/{SESSION}/{YYYYMMDD}

    pip install curl_cffi requests python-dotenv

    python bms_venue_watch.py --probe    # calibrate FIRST
    python bms_venue_watch.py            # run continuously
    python bms_venue_watch.py --once     # single cycle, for cron / Actions
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass

try:
    from curl_cffi import requests as cffi_requests
    HAVE_CFFI = True
except ImportError:
    HAVE_CFFI = False


# ---------------------------------------------------------------- config

MOVIE_NAME = os.getenv("MOVIE_NAME", "My Movie")
MOVIE_SLUG = os.getenv("MOVIE_SLUG", "").strip()        # spider-man-brand-new-day
EVENT_CODE = os.getenv("EVENT_CODE", "").strip()        # ET00502600
CITY_SLUG  = os.getenv("CITY_SLUG", "chennai")
CITY_CODE  = os.getenv("CITY_CODE", "chen").lower()     # used in seat-layout URLs
LANGUAGE   = os.getenv("LANGUAGE", "").strip().lower()  # english / tamil / "" = all

# Explicit dates win. Otherwise a rolling window from today.
TARGET_DATES = [d.strip() for d in os.getenv("TARGET_DATES", "").split(",") if d.strip()]
DAYS_AHEAD   = int(os.getenv("DAYS_AHEAD", "5"))

# EXACT venue codes, e.g. "PCAN,AGSM,PVPZ". Preferred - codes are stable.
WATCH_CODES = [c.strip().lower() for c in os.getenv("WATCH_CODES", "").split(",") if c.strip()]

# Substring match on venue NAME, e.g. "sathyam,rohini". Fuzzier fallback.
WATCH_VENUES = [v.strip().lower() for v in os.getenv("WATCH_VENUES", "").split(",") if v.strip()]

# Both empty = alert on every new venue.

ALERT_ON_MORE_SHOWS = os.getenv("ALERT_ON_MORE_SHOWS", "true").lower() == "true"

POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "150"))
JITTER_SECONDS = int(os.getenv("JITTER_SECONDS", "40"))
REQUEST_GAP    = float(os.getenv("REQUEST_GAP", "3"))
STATE_FILE     = Path(os.getenv("STATE_FILE", "bms_venue_state.json"))

NTFY_TOPIC  = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOKEN  = os.getenv("NTFY_TOKEN", "").strip()

CALLMEBOT_PHONE  = os.getenv("CALLMEBOT_PHONE", "").strip()
CALLMEBOT_APIKEY = os.getenv("CALLMEBOT_APIKEY", "").strip()

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "en-IN,en-GB;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Referer": f"https://in.bookmyshow.com/movies/{CITY_SLUG}/{MOVIE_SLUG}/{EVENT_CODE}",
}

MOVIE_PAGE = f"https://in.bookmyshow.com/movies/{CITY_SLUG}/{MOVIE_SLUG}/{EVENT_CODE}"

VENUE_NAME_RE = re.compile(r"(venue|theatre|theater|cinema).{0,3}name", re.I)
VENUE_CODE_RE = re.compile(r"(venue|theatre|theater|cinema).{0,3}code", re.I)
SHOW_KEY_RE   = re.compile(r"(show ?times?|show_times?|shows|sessions|showings)", re.I)


def log(m):
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def showtimes_url(date_str):
    base = (f"https://in.bookmyshow.com/movies/{CITY_SLUG}/{MOVIE_SLUG}"
            f"/buytickets/{EVENT_CODE}/{date_str}")
    q = {"etCodes": EVENT_CODE, "refEventCode": EVENT_CODE}
    if LANGUAGE:
        q["language"] = LANGUAGE
    return f"{base}?{urlencode(q)}"


def seat_layout_url(venue_code, session_id, date_str):
    return (f"https://in.bookmyshow.com/movies/{CITY_CODE}/seat-layout"
            f"/{EVENT_CODE}/{venue_code}/{session_id}/{date_str}")


def dates_to_watch():
    if TARGET_DATES:
        return TARGET_DATES
    today = datetime.now()
    return [(today + timedelta(days=i)).strftime("%Y%m%d") for i in range(DAYS_AHEAD)]


# ---------------------------------------------------------------- fetch

_SESSION = None


def _new_session():
    """Fresh session, warmed up on the movie page so Akamai cookies are set."""
    if HAVE_CFFI:
        sess = cffi_requests.Session(impersonate="chrome124")
    else:
        sess = requests.Session()
    try:
        h = dict(HEADERS)
        h["Sec-Fetch-Site"] = "none"
        h.pop("Referer", None)
        r = sess.get(MOVIE_PAGE, headers=h, timeout=25)
        log(f"warm-up: HTTP {r.status_code}, {len(sess.cookies)} cookies")
        time.sleep(2)
    except Exception as e:
        log(f"warm-up failed: {e}")
    return sess


def fetch(url, _retry=True):
    global _SESSION
    if _SESSION is None:
        _SESSION = _new_session()
    r = _SESSION.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
    if r.status_code == 403 and _retry:
        log("403 - rebuilding session and retrying once")
        time.sleep(5)
        _SESSION = _new_session()
        return fetch(url, _retry=False)
    return r.status_code, (r.text or "")


# ---------------------------------------------------------------- extraction

def _json_blobs(html):
    for m in re.finditer(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S):
        try:
            yield json.loads(m.group(1))
        except Exception:
            pass
    for m in re.finditer(r'window\.__[A-Z_]+__\s*=\s*(\{.*?\});?\s*</script>', html, re.S):
        try:
            yield json.loads(m.group(1))
        except Exception:
            pass


def _walk(node, out):
    """Collect {key: {name, code, shows}} from arbitrary nested JSON."""
    if isinstance(node, dict):
        name = code = None
        for k, v in node.items():
            if not isinstance(v, str) or not v.strip():
                continue
            if name is None and VENUE_NAME_RE.search(k):
                name = v.strip()
            elif code is None and VENUE_CODE_RE.search(k) and 2 <= len(v.strip()) <= 12:
                code = v.strip()

        if name:
            shows = 0
            for k, v in node.items():
                if SHOW_KEY_RE.search(k) and isinstance(v, list):
                    shows = max(shows, len(v))
            key = code or name
            prev = out.get(key, {"name": name, "code": code, "shows": 0})
            out[key] = {"name": name,
                        "code": code or prev.get("code"),
                        "shows": max(prev["shows"], shows)}

        for v in node.values():
            _walk(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out)


def _approx_counts(html, out):
    """BookMyShow keeps showtimes in a structure separate from the venue dict,
    so there is no showtime list next to venueName. As a proxy, count how often
    each venue code appears in the payload - it rises as sessions are added.
    Approximate and noisy: good for eyeballing, not for alerting."""
    for info in out.values():
        code = info.get("code")
        if code:
            info["shows"] = html.count(f'"{code}"')
            info["approx"] = True


def extract_venues(html):
    out = {}
    for blob in _json_blobs(html):
        _walk(blob, out)

    if out and all(i.get("shows", 0) == 0 for i in out.values()):
        _approx_counts(html, out)

    if not out:  # regex fallback
        for m in re.finditer(r'"[Vv]enue[Nn]ame"\s*:\s*"([^"]{3,120})"', html):
            out.setdefault(m.group(1).strip(), {"name": m.group(1).strip(),
                                                "code": None, "shows": 0})
        for m in re.finditer(r'data-venue-name="([^"]{3,120})"', html):
            out.setdefault(m.group(1).strip(), {"name": m.group(1).strip(),
                                                "code": None, "shows": 0})
    return out


def is_watched(info):
    if not WATCH_CODES and not WATCH_VENUES:
        return True
    code = (info.get("code") or "").lower()
    if code and code in WATCH_CODES:          # exact, not substring
        return True
    name = (info.get("name") or "").lower()
    return any(w in name for w in WATCH_VENUES)


def label(info):
    c = info.get("code")
    return f"{info['name']} [{c}]" if c else info["name"]


def shows_str(info):
    n = info.get("shows", 0)
    return f"~{n} refs" if info.get("approx") else f"{n} shows"


# ---------------------------------------------------------------- alerts

def notify(title, body, click_date=None, priority="urgent"):
    url = showtimes_url(click_date or dates_to_watch()[0])
    if NTFY_TOPIC:
        h = {"Title": title, "Priority": priority,
             "Tags": "rotating_light,ticket", "Click": url}
        if NTFY_TOKEN:
            h["Authorization"] = f"Bearer {NTFY_TOKEN}"
        try:
            requests.post(f"{NTFY_SERVER}/{NTFY_TOPIC}", data=body.encode("utf-8"),
                          headers=h, timeout=15).raise_for_status()
            log("ntfy sent")
        except Exception as e:
            log(f"ntfy FAILED: {e}")

    if CALLMEBOT_PHONE and CALLMEBOT_APIKEY:
        try:
            requests.get("https://api.callmebot.com/whatsapp.php",
                         params={"phone": CALLMEBOT_PHONE,
                                 "text": f"{title}\n{body}\n{url}",
                                 "apikey": CALLMEBOT_APIKEY},
                         timeout=20).raise_for_status()
        except Exception as e:
            log(f"whatsapp FAILED: {e}")


# ---------------------------------------------------------------- state

def load_state():
    if STATE_FILE.exists():
        try:
            s = json.loads(STATE_FILE.read_text())
            if s.get("schema") == 2:
                return s
            log("old state schema - rebaselining")
        except Exception:
            pass
    return {"schema": 2, "snapshot": {}, "cycles": 0}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))


# ---------------------------------------------------------------- probe

def _find_sample(node, found):
    """Locate the first dict containing a venue name and describe its keys."""
    if found:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and VENUE_NAME_RE.search(k) and v.strip():
                shape = {}
                for kk, vv in node.items():
                    if isinstance(vv, list):
                        shape[kk] = f"<list len={len(vv)}>"
                    elif isinstance(vv, dict):
                        shape[kk] = f"<dict keys={list(vv)[:5]}>"
                    else:
                        shape[kk] = repr(vv)[:50]
                found.append((v.strip(), shape))
                return
        for v in node.values():
            _find_sample(v, found)
    elif isinstance(node, list):
        for v in node:
            _find_sample(v, found)


def dump_keys(html):
    found = []
    for blob in _json_blobs(html):
        _find_sample(blob, found)
        if found:
            break
    if not found:
        print("  could not locate a venue dict")
        return
    name, shape = found[0]
    print(f"\n  --- keys on the dict for '{name}' ---")
    for k, v in sorted(shape.items()):
        print(f"    {k:<28} {v}")
    print("  -> whichever key holds the showtime list goes into SHOW_KEY_RE\n")


def probe(show_keys=False):
    for d in dates_to_watch()[:2]:
        url = showtimes_url(d)
        print(f"\n=== {d} ===\n{url}")
        try:
            status, html = fetch(url)
        except Exception as e:
            print(f"  fetch error: {e}")
            continue
        print(f"  HTTP {status}, {len(html)} bytes")
        venues = extract_venues(html)
        if not venues:
            Path(f"debug_{d}.html").write_text(html, encoding="utf-8")
            print(f"  NO VENUES FOUND -> wrote debug_{d}.html")
        else:
            for k, info in sorted(venues.items(), key=lambda x: x[1]["name"]):
                mark = "*" if is_watched(info) else " "
                print(f"  {mark} {label(info)}  ({shows_str(info)})")
            print(f"  -- {len(venues)} venues")
        if show_keys:
            dump_keys(html)
        time.sleep(REQUEST_GAP)
    print("\nVenues marked * will alert. Tune WATCH_VENUES in .env.")
    print("Prefer venue CODES (e.g. PBRM) over names - they don't get renamed.")


# ---------------------------------------------------------------- main

def cycle(state):
    snapshot = state["snapshot"]
    events = []
    latest_date = None

    for d in dates_to_watch():
        try:
            status, html = fetch(showtimes_url(d))
        except Exception as e:
            log(f"{d}: fetch error {e}")
            continue

        if status in (403, 429):
            log(f"{d}: blocked ({status}) - skipping")
            time.sleep(REQUEST_GAP * 3)
            continue
        if status == 404:
            continue

        venues = extract_venues(html)
        if not venues:
            log(f"{d}: 0 venues parsed (HTTP {status}) - extractor may be stale")
            time.sleep(REQUEST_GAP)
            continue

        if WATCH_CODES:
            live = {(i.get("code") or "").lower() for i in venues.values()}
            open_  = sorted(c.upper() for c in WATCH_CODES if c in live)
            wait_  = sorted(c.upper() for c in WATCH_CODES if c not in live)
            log(f"{d}: {len(venues)} venues | OPEN {open_ or '-'} | WAITING {wait_ or '-'}")

        prev = snapshot.get(d)
        if prev is None:
            snapshot[d] = venues
            log(f"{d}: baseline {len(venues)} venues")
            time.sleep(REQUEST_GAP)
            continue

        for k, info in venues.items():
            if not is_watched(info):
                continue
            if k not in prev:
                events.append(f"NEW THEATRE  {label(info)} - {shows_str(info)} on {d}")
                latest_date = d
            elif (ALERT_ON_MORE_SHOWS and not info.get("approx")
                  and info["shows"] > prev[k].get("shows", 0)):
                events.append(f"MORE SHOWS   {label(info)} - "
                              f"{prev[k]['shows']} -> {info['shows']} on {d}")
                latest_date = d

        snapshot[d] = venues
        log(f"{d}: {len(venues)} venues")
        time.sleep(REQUEST_GAP)

    return events, latest_date


def run_cycle(state):
    state["cycles"] += 1
    events, d = cycle(state)
    if events:
        body = "\n".join(events)
        log(">>> " + body.replace("\n", " | "))
        notify(f"{MOVIE_NAME}: {len(events)} new", body, click_date=d)
    save_state(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="dump parsed venues and exit")
    ap.add_argument("--once", action="store_true", help="single cycle, for cron")
    ap.add_argument("--keys", action="store_true",
                    help="with --probe: dump the venue dict's keys")
    ap.add_argument("--test-alert", action="store_true",
                    help="fire a real alert using your live config, then exit")
    args = ap.parse_args()

    if not (MOVIE_SLUG and EVENT_CODE):
        sys.exit("Set MOVIE_SLUG and EVENT_CODE in .env")
    if not HAVE_CFFI:
        log("WARNING: curl_cffi not installed - expect 403s")

    if args.probe:
        probe(show_keys=args.keys)
        return

    if args.test_alert:
        if not NTFY_TOPIC:
            sys.exit("NTFY_TOPIC is empty - the .env is not being read")
        log(f"posting to {NTFY_SERVER}/{NTFY_TOPIC[:4]}...")
        notify(f"TEST: {MOVIE_NAME}",
               "NEW THEATRE  PVR: Test Venue [TEST] - 4 shows on 20260730",
               click_date=dates_to_watch()[0])
        log("if your phone did not alarm, fix this before relying on it")
        return

    if not NTFY_TOPIC:
        sys.exit("Set NTFY_TOPIC in .env")

    state = load_state()
    log(f"Watching {MOVIE_NAME} | {CITY_SLUG} | dates {dates_to_watch()}")
    if WATCH_CODES or WATCH_VENUES:
        bits = []
        if WATCH_CODES:
            bits.append("codes=" + ",".join(c.upper() for c in WATCH_CODES))
        if WATCH_VENUES:
            bits.append("names~" + ",".join(WATCH_VENUES))
        log("Filter: " + " | ".join(bits))
    else:
        log("Filter: ALL venues (no WATCH_CODES / WATCH_VENUES set)")

    if args.once:
        run_cycle(state)
        if state["cycles"] % 200 == 0:
            notify("watcher alive", f"cycle {state['cycles']}", priority="min")
        return

    while True:
        run_cycle(state)
        time.sleep(POLL_SECONDS + random.randint(0, JITTER_SECONDS))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped")


# ===================================================================
# .env
# ===================================================================
# MOVIE_NAME=Spider-Man: Brand New Day
# MOVIE_SLUG=spider-man-brand-new-day
# EVENT_CODE=ET00502600
# CITY_SLUG=chennai
# CITY_CODE=chen
# TARGET_DATES=20260730,20260731,20260801
#
# # PCAN = PVR VR Chennai, AGSM = AGS Maduravoyal, PVPZ = PVR Palazzo Forum
# WATCH_CODES=PCAN,AGSM,PVPZ
# WATCH_VENUES=
#
# NTFY_TOPIC=bms-n-4kq7xz91
# POLL_SECONDS=150
# ===================================================================
