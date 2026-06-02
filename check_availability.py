#!/usr/bin/env python3
"""
Clic Sante availability monitor.

Watches a Clic Sante clinic for new appointment slots and sends a phone push
(via ntfy) the instant one appears.

IMPORTANT - which data we read:
  The booking page renders its calendar from the `schedules/day` API using the
  establishment's *resolved* service ids (the "portalServicesUnified" ids in the
  URL are aggregates that must be resolved per establishment). We replicate
  exactly that, so we see the same slots the page shows AFTER its 2 screening
  questions. (The screening questions are a UI/registration gate; they do not
  change which slots exist.) We do NOT use the `/availabilities` endpoint -- that
  one needs a resource list and silently returns [] without it.

Health / self-validation:
  * On any check error -> throttled "monitor error" push (silence never means
    "no slots").
  * Optional daily heartbeat push (HEARTBEAT_PUSH=1).
  * Optional dead-man's switch ping (HEALTHCHECK_URL).
  * `--selftest` fires a clearly-labelled fake-slot push to validate the pipeline.

State (state.json) records seen slot ids so we only alert on NEW slots.
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

BOOKING_URL = env(
    "CS_BOOKING_URL",
    "https://clients3.clicsante.ca/8154/take-appt?portalPlace=23139&portalPostalCode=null"
    "&lang=fr&portalServicesUnified=11,289,336,354&portalEst=408574&locale=fr",
)

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

    If empty_on_nothing=True, a 404 'availabilities.public.nothing-for-day'
    (how schedules/day signals 'no slots') is returned as an empty result,
    not an error.
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
            # schedules/day signals "no slots" with a 404 (various internal codes),
            # so for those calls any 404 means empty, not a real error.
            if empty_on_nothing and e.code == 404:
                return {"availabilities": []}
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


def fetch_slots():
    today = dt.date.today()
    stop = today + dt.timedelta(days=LOOKAHEAD_DAYS)
    services = resolve_service_ids()
    if not services:
        raise RuntimeError("could not resolve any service ids (site change?)")
    slots = []
    for place in PLACES:
        for svc in services:
            url = (f"{API}/{ESTABLISHMENT}/schedules/day"
                   f"?dateStart={today.isoformat()}&dateStop={stop.isoformat()}"
                   f"&service={svc}&timezone={TIMEZONE}&places={place}&gapMode=false")
            data = http_json(url, empty_on_nothing=True)
            for s in (data.get("availabilities") or []):
                slots.append(s)
    return slots


def slot_sig(slot):
    if isinstance(slot, dict) and slot.get("id") is not None:
        return f"id:{slot['id']}"
    return json.dumps(slot, sort_keys=True, ensure_ascii=False)


def fmt_slot(slot):
    start = (slot.get("start") or "")[:16].replace("T", " ")  # "YYYY-MM-DD HH:MM"
    place = str(slot.get("place", ""))
    where = PLACE_NAMES.get(place, f"place {place}")
    return f"{start}  -  {where}".strip()


def human_summary(slots, limit=12):
    lines = sorted({fmt_slot(s) for s in slots})
    out = lines[:limit]
    if len(lines) > limit:
        out.append(f"... +{len(lines) - limit} more")
    return "\n".join(out)


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


def run_check():
    print("[info]", now_utc().isoformat(), "establishment", ESTABLISHMENT,
          "places", PLACES, "unified", UNIFIED_SERVICES)
    slots = fetch_slots()
    now_sigs = sorted({slot_sig(s) for s in slots})

    state = load_state()
    prev = set(state.get("signatures", []))
    new_sigs = [s for s in now_sigs if s not in prev]
    print(f"[info] slots now: {len(slots)} | new vs last run: {len(new_sigs)}")

    if slots and new_sigs:
        new_slots = [s for s in slots if slot_sig(s) in set(new_sigs)]
        title = f"Rendez-vous dispo! ({len(new_slots)})"
        message = (f"{len(new_slots)} nouveau(x) creneau(x) a Clic Sante.\n"
                   f"{human_summary(new_slots)}\n\nReserver ->")
        send_push(title, message)

    state["signatures"] = now_sigs
    state["count"] = len(slots)
    if slots:
        state["last_seen"] = now_utc().isoformat()
    state["last_check"] = now_utc().isoformat()
    state["last_status"] = "ok"
    state.pop("last_error", None)
    state["heartbeat"] = dt.date.today().isoformat()
    save_state(state)
    return state


def main():
    if "--test-push" in sys.argv:
        send_push("Clic Sante monitor: test",
                  "If you can read this on your phone, notifications work.",
                  tags="white_check_mark")
        return 0

    if "--selftest" in sys.argv:
        sample = [{"id": "SELFTEST-1", "place": "23139", "start": "2026-06-15T09:30:00+00:00"},
                  {"id": "SELFTEST-2", "place": "23139", "start": "2026-06-16T14:00:00+00:00"}]
        send_push("[TEST] Rendez-vous dispo! (2)",
                  "Ceci est un test du systeme.\n"
                  f"{human_summary(sample)}\n\n(ignore - validation only)",
                  tags="test_tube")
        print("[selftest] sent a labelled availability push (no real slots).")
        return 0

    try:
        state = run_check()
    except Exception as e:  # noqa: BLE001 - alert on watcher failure, then fail loud
        msg = str(e)
        print(f"[error] check failed: {msg}", file=sys.stderr)
        st = load_state()
        last = st.get("last_error_push")
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
            st["last_error_push"] = now_utc().isoformat()
        st["last_check"] = now_utc().isoformat()
        st["last_status"] = "error"
        st["last_error"] = msg[:500]
        save_state(st)
        ping_healthcheck("/fail")
        raise

    ping_healthcheck()

    if HEARTBEAT_PUSH and state.get("heartbeat_push_date") != dt.date.today().isoformat():
        send_push("Clic Sante monitor OK",
                  f"Still watching. {state.get('count', 0)} slot(s) right now.",
                  tags="white_check_mark", priority="min")
        state["heartbeat_push_date"] = dt.date.today().isoformat()
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
