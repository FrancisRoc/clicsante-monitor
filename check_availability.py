#!/usr/bin/env python3
"""
Clic Sante availability monitor.

Watches a Clic Sante clinic for new appointment days and sends a phone push
(via ntfy) the instant one appears.

WHICH DATA WE READ -- we mirror the booking page exactly:
  The page's calendar is drawn from the `schedules/public` endpoint. It returns
  two lists per service:
    * `availabilities` -> days that are OPEN and bookable (what the calendar
      lets you click)
    * `daysComplete`   -> days that exist but are FULL (greyed out)
  We alert ONLY on `availabilities`, so "the monitor says there's a slot" means
  "the page would let you book that day". The URL's `portalServicesUnified` ids
  are aggregates; we resolve them to this establishment's real service ids first
  (same as the page does). The 2 screening questions are a UI gate only -- they
  do NOT change which days `schedules/public` returns (verified against a live
  clinic), so we don't need them.

  We deliberately do NOT use `/availabilities` (needs a resource list, silently
  returns []) nor trust `schedules/day` alone (it can surface times on days the
  page marks complete).

Health / self-validation:
  * On any check error -> throttled "monitor error" push (silence never means
    "no slots").
  * Optional daily heartbeat push (HEARTBEAT_PUSH=1).
  * Optional dead-man's switch ping (HEALTHCHECK_URL).
  * `--selftest` fires a clearly-labelled fake-slot push to validate the pipeline.

State (state.json) records seen (place, date) pairs so we only alert on NEW days.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
import datetime as dt


def env(name, default=""):
    """Read env var, treating unset OR empty-string as 'use default'
    (GitHub Actions expands ${{ vars.X }} to '' when unset)."""
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# ---- What to watch (from the booking URL) ----
API = "https://api3.clicsante.ca/v3/establishments"
ESTABLISHMENT = env("CS_ESTABLISHMENT", "8154")
PLACES = [p.strip() for p in env("CS_PLACES", env("CS_PLACE", "23139")).split(",") if p.strip()]
UNIFIED_SERVICES = [s.strip() for s in env("CS_SERVICES", "11,289,336,354").split(",") if s.strip()]
TIMEZONE = env("CS_TIMEZONE", "America/Toronto")
LOOKAHEAD_DAYS = int(env("CS_LOOKAHEAD_DAYS", "90"))

# Friendly names for this establishment's places (for nicer notifications).
PLACE_NAMES = {
    "23139": "920 Bd du Seminaire N", "23140": "3120 boul. Taschereau",
    "23141": "1333 Boul. Jacques-Cartier E", "23142": "150 Rue Saint-Thomas",
    "23143": "2750 Boul. Laframboise, St-Hyacinthe", "23144": "200 Bd Brisebois",
    "24863": "920 blv du Seminaire nord",
}


def default_booking_url():
    """Build the take-appt link for the watched clinic so a push always opens
    the right page (no hard-coded mismatch when watching a different clinic)."""
    return (f"https://clients3.clicsante.ca/{ESTABLISHMENT}/take-appt"
            f"?portalPlace={PLACES[0] if PLACES else ''}&portalPostalCode=null"
            f"&lang=fr&portalServicesUnified={','.join(UNIFIED_SERVICES)}&locale=fr")


BOOKING_URL = env("CS_BOOKING_URL", default_booking_url())

# ---- Notification (ntfy) ----
NTFY_SERVER = env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = env("NTFY_TOPIC", "")
NTFY_TOKEN = env("NTFY_TOKEN", "")

# ---- Health / observability ----
HEARTBEAT_PUSH = env("HEARTBEAT_PUSH", "") not in ("", "0", "false", "no")
HEALTHCHECK_URL = env("HEALTHCHECK_URL", "")
ERROR_PUSH_THROTTLE_MIN = int(env("CS_ERROR_THROTTLE_MIN", "60"))

STATE_FILE = env("CS_STATE_FILE", "state.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://clients3.clicsante.ca",
    "Referer": "https://clients3.clicsante.ca/",
}


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def http_json(url, tries=4, empty_on_nothing=False):
    """GET JSON with retries.

    If empty_on_nothing=True, a 404 (how some service ids signal 'nothing here')
    is returned as an empty result, not an error.
    """
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8")
            except Exception:
                pass
            if empty_on_nothing and e.code == 404:
                return {"availabilities": [], "daysComplete": []}
            last = f"HTTP {e.code}: {body[:200]}"
        except Exception as e:  # noqa: BLE001 - transient -> retry
            last = str(e)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET failed after {tries} tries ({url}): {last}")


def resolve_service_ids():
    """Resolve the unified service ids in the URL to this establishment's real
    service ids (what the schedule endpoints require)."""
    ids = set()
    for u in UNIFIED_SERVICES:
        data = http_json(f"{API}/{ESTABLISHMENT}/unified/{u}/service")
        if isinstance(data, list):
            ids.update(str(x) for x in data)
    return sorted(ids)


def _as_date(item):
    """Normalise a schedules/public availability entry to a 'YYYY-MM-DD' string."""
    if isinstance(item, dict):
        for k in ("date", "day", "start", "datetime"):
            if item.get(k):
                return str(item[k])[:10]
        return str(item)[:10]
    return str(item)[:10]


def fetch_available_days():
    """Return {"place|date": {"place","date","services":set()}} for every day
    the page would show as bookable (availabilities), across all places/services."""
    today = dt.date.today()
    stop = today + dt.timedelta(days=LOOKAHEAD_DAYS)
    services = resolve_service_ids()
    if not services:
        raise RuntimeError("could not resolve any service ids (site change?)")
    found = {}
    for place in PLACES:
        for svc in services:
            url = (f"{API}/{ESTABLISHMENT}/schedules/public"
                   f"?dateStart={today.isoformat()}&dateStop={stop.isoformat()}"
                   f"&service={svc}&timezone={TIMEZONE}&places={place}")
            data = http_json(url, empty_on_nothing=True)
            for a in (data.get("availabilities") or []):
                date = _as_date(a)
                sig = f"{place}|{date}"
                found.setdefault(sig, {"place": place, "date": date,
                                       "services": set()})["services"].add(svc)
    return found


def fetch_times_for(place, date, service):
    """Best-effort: list a few clock times for a newly-open day (for the push
    body). Never raises -- enrichment only."""
    try:
        url = (f"{API}/{ESTABLISHMENT}/schedules/day"
               f"?dateStart={date}&dateStop={date}&service={service}"
               f"&timezone={TIMEZONE}&places={place}&gapMode=false")
        data = http_json(url, empty_on_nothing=True)
        times = sorted({(s.get("start") or "")[11:16]
                        for s in (data.get("availabilities") or []) if s.get("start")})
        return [t for t in times if t]
    except Exception:
        return []


def where(place):
    return PLACE_NAMES.get(str(place), f"place {place}")


def fmt_day(entry, with_times=True):
    line = f"{entry['date']}  -  {where(entry['place'])}"
    if with_times:
        svc = sorted(entry["services"])[0] if entry["services"] else None
        times = fetch_times_for(entry["place"], entry["date"], svc) if svc else []
        if times:
            shown = ", ".join(times[:6]) + (f" +{len(times) - 6}" if len(times) > 6 else "")
            line += f"  ({shown})"
    return line


def human_summary(entries, limit=12, with_times=True):
    entries = sorted(entries, key=lambda e: (e["date"], str(e["place"])))
    lines = [fmt_day(e, with_times) for e in entries[:limit]]
    if len(entries) > limit:
        lines.append(f"... +{len(entries) - limit} more day(s)")
    return "\n".join(lines)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"signatures": [], "count": 0, "last_seen": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def send_push(title, message, tags="bell,calendar", priority="high"):
    if not NTFY_TOPIC:
        print("[warn] NTFY_TOPIC not set - skipping push:", title, "/", message)
        return
    headers = {
        "Title": title.encode("ascii", "replace").decode("ascii"),
        "Priority": priority,
        "Tags": tags,
        "Click": BOOKING_URL,   # booking URL has commas -> can't use Actions header
    }
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    req = urllib.request.Request(f"{NTFY_SERVER}/{NTFY_TOPIC}",
                                 data=message.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"[push] sent ({r.status})")


def ping_healthcheck(suffix=""):
    if not HEALTHCHECK_URL:
        return
    try:
        urllib.request.urlopen(HEALTHCHECK_URL + suffix, timeout=10).read()
    except Exception as e:  # noqa: BLE001 - best-effort
        print(f"[warn] healthcheck ping failed: {e}")


def do_check(state):
    """Run one availability check against `state` (a dict), push on new open
    days, and update `state` in place. Returns `state`. Raises on fetch error
    (caller decides how to alert)."""
    print("[info]", now_utc().isoformat(), "establishment", ESTABLISHMENT,
          "places", PLACES, "unified", UNIFIED_SERVICES)
    found = fetch_available_days()
    now_sigs = sorted(found.keys())

    prev = set(state.get("signatures", []))
    new_sigs = [s for s in now_sigs if s not in prev]
    print(f"[info] open days now: {len(now_sigs)} | new vs last check: {len(new_sigs)}")

    if new_sigs:
        new_entries = [found[s] for s in new_sigs]
        title = f"Rendez-vous dispo! ({len(new_entries)})"
        message = (f"{len(new_entries)} jour(s) ouvert(s) a Clic Sante.\n"
                   f"{human_summary(new_entries)}\n\nReserver ->")
        send_push(title, message)

    state["signatures"] = now_sigs
    state["count"] = len(now_sigs)
    if now_sigs:
        state["last_seen"] = now_utc().isoformat()
    state["last_check"] = now_utc().isoformat()
    state["last_status"] = "ok"
    state.pop("last_error", None)
    state["heartbeat"] = dt.date.today().isoformat()
    return state


def maybe_error_push(state, msg):
    """Send a throttled 'monitor error' push and record it in `state`."""
    last = state.get("last_error_push")
    throttled = False
    if last:
        try:
            throttled = (now_utc() - dt.datetime.fromisoformat(last)).total_seconds() \
                < ERROR_PUSH_THROTTLE_MIN * 60
        except ValueError:
            throttled = False
    if not throttled:
        send_push("Clic Sante monitor ERROR",
                  f"The watcher failed:\n{msg[:300]}\nIt keeps retrying; check GitHub Actions.",
                  tags="warning", priority="default")
        state["last_error_push"] = now_utc().isoformat()
    state["last_check"] = now_utc().isoformat()
    state["last_status"] = "error"
    state["last_error"] = msg[:500]


def run_check():
    state = load_state()
    try:
        do_check(state)
    except Exception:
        save_state(state)
        raise
    save_state(state)
    return state


def run_loop(interval, max_seconds):
    """Long-lived in-process loop: check every `interval` s for up to
    `max_seconds`, keeping dedup state in memory (no duplicate pings) and never
    dying on a transient error. State is persisted each iteration so the next
    job (and a final git commit) can resume. Designed to be re-launched before
    the GitHub 6h job limit, so it doesn't rely on GitHub's flaky scheduler."""
    deadline = time.time() + max_seconds
    state = load_state()
    n = 0
    while True:
        n += 1
        try:
            do_check(state)
        except Exception as e:  # noqa: BLE001 - keep looping on transient errors
            print(f"[error] check #{n} failed: {e}", file=sys.stderr)
            maybe_error_push(state, str(e))
        save_state(state)
        ping_healthcheck()
        if time.time() + interval >= deadline:
            print(f"[loop] reached time budget after {n} checks; exiting to relaunch.")
            return 0
        time.sleep(interval)


def main():
    if "--test-push" in sys.argv:
        send_push("Clic Sante monitor: test",
                  "If you can read this on your phone, notifications work.",
                  tags="white_check_mark")
        return 0

    if "--selftest" in sys.argv:
        sample = [{"place": "23139", "date": "2026-06-15", "services": set()},
                  {"place": "23139", "date": "2026-06-16", "services": set()}]
        send_push("[TEST] Rendez-vous dispo! (2)",
                  "Ceci est un test du systeme.\n"
                  f"{human_summary(sample, with_times=False)}\n\n(ignore - validation only)",
                  tags="test_tube")
        print("[selftest] sent a labelled availability push (no real slots).")
        return 0

    # Long-lived loop mode: `--loop [interval_seconds] [max_seconds]`.
    if "--loop" in sys.argv:
        i = sys.argv.index("--loop")
        rest = [a for a in sys.argv[i + 1:] if not a.startswith("-")]
        interval = int(rest[0]) if len(rest) > 0 else int(env("CS_LOOP_INTERVAL", "600"))
        max_seconds = int(rest[1]) if len(rest) > 1 else int(env("CS_LOOP_MAX_SECONDS", "19200"))  # 5h20m
        print(f"[loop] starting: every {interval}s for up to {max_seconds}s")
        return run_loop(interval, max_seconds)

    try:
        state = run_check()
    except Exception as e:  # noqa: BLE001 - alert on watcher failure, then fail loud
        print(f"[error] check failed: {e}", file=sys.stderr)
        st = load_state()
        maybe_error_push(st, str(e))
        save_state(st)
        ping_healthcheck("/fail")
        raise

    ping_healthcheck()

    if HEARTBEAT_PUSH and state.get("heartbeat_push_date") != dt.date.today().isoformat():
        send_push("Clic Sante monitor OK",
                  f"Still watching. {state.get('count', 0)} open day(s) right now.",
                  tags="white_check_mark", priority="min")
        state["heartbeat_push_date"] = dt.date.today().isoformat()
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
