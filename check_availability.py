#!/usr/bin/env python3
"""
Clic Sante availability monitor.

Polls the Clic Sante availabilities API for a specific establishment/place/services
and sends a phone push (via ntfy) the moment NEW availabilities appear.

We query the JSON data API directly (not the booking web page), so the page's
"refresh restarts the questionnaire" behaviour does not affect us.

Health / self-validation:
  * On any check error it pushes a throttled "monitor error" alert (so silence
    is never mistaken for "no slots").
  * Optional daily heartbeat push (HEARTBEAT_PUSH=1) = positive "still alive".
  * Optional dead-man's switch ping (HEALTHCHECK_URL) for an external watchdog.
  * `--selftest` fires a real (clearly-labelled) availability push so you can
    confirm the whole detect->notify pipeline end to end.

State is kept in a small JSON file so we only alert on genuinely new slots.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
import datetime as dt


def env(name, default=""):
    """Read an env var, treating unset OR empty-string as "use default".

    GitHub Actions expands ${{ vars.X }} to "" when the variable doesn't exist,
    so we must not let an empty string override the baked-in defaults.
    """
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# ---- What to watch (from the booking URL the user provided) ----
ESTABLISHMENT = env("CS_ESTABLISHMENT", "8154")
PLACE         = env("CS_PLACE", "23139")
SERVICES      = env("CS_SERVICES", "11,289,336,354")
TIMEZONE      = env("CS_TIMEZONE", "America/Toronto")
LOOKAHEAD_DAYS = int(env("CS_LOOKAHEAD_DAYS", "90"))

BOOKING_URL = env(
    "CS_BOOKING_URL",
    "https://clients3.clicsante.ca/8154/take-appt?portalPlace=23139&portalPostalCode=null"
    "&lang=fr&portalServicesUnified=11,289,336,354&portalEst=408574&locale=fr",
)

# ---- Notification (ntfy) ----
NTFY_SERVER = env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC  = env("NTFY_TOPIC", "")          # required to actually send
NTFY_TOKEN  = env("NTFY_TOKEN", "")          # optional (for protected topics)

# ---- Health / observability ----
HEARTBEAT_PUSH = env("HEARTBEAT_PUSH", "") not in ("", "0", "false", "no")
HEALTHCHECK_URL = env("HEALTHCHECK_URL", "")   # e.g. a healthchecks.io ping URL
ERROR_PUSH_THROTTLE_MIN = int(env("CS_ERROR_THROTTLE_MIN", "60"))

STATE_FILE = env("CS_STATE_FILE", "state.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def api_url():
    today = dt.date.today()
    stop = today + dt.timedelta(days=LOOKAHEAD_DAYS)
    return (
        f"https://api3.clicsante.ca/v3/establishments/{ESTABLISHMENT}/availabilities"
        f"?dateStart={today.isoformat()}&dateStop={stop.isoformat()}"
        f"&places={PLACE}&services={SERVICES}&resources=&timezone={TIMEZONE}"
    )


def http_get_json(url, tries=4):
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://clients3.clicsante.ca",
            "Referer": "https://clients3.clicsante.ca/",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8")
                return json.loads(body)
        except Exception as e:  # noqa: BLE001 - transient network/HTTP -> retry
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {last}")


def signature(item):
    if isinstance(item, dict):
        for key in ("id", "availabilityId", "uuid"):
            if item.get(key) is not None:
                return f"{key}:{item[key]}"
        return json.dumps(item, sort_keys=True, ensure_ascii=False)
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


def human_summary(items, limit=10):
    lines = []
    for it in items[:limit]:
        if isinstance(it, dict):
            date = (it.get("date") or it.get("day") or it.get("availabilityDate")
                    or it.get("start") or it.get("startDate") or "")
            tm = (it.get("time") or it.get("startTime") or it.get("hour") or "")
            label = f"{date} {tm}".strip()
            lines.append(label or json.dumps(it, ensure_ascii=False)[:80])
        else:
            lines.append(str(it)[:80])
    extra = len(items) - len(lines)
    if extra > 0:
        lines.append(f"... +{extra} more")
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
        print("[warn] NTFY_TOPIC not set - skipping push. Message was:")
        print(title, "/", message)
        return
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    # ntfy header values must be ASCII; the "Actions" shorthand is comma-delimited
    # and the booking URL contains commas, so we rely on the tappable "Click".
    headers = {
        "Title": title.encode("ascii", "replace").decode("ascii"),
        "Priority": priority,
        "Tags": tags,
        "Click": BOOKING_URL,
    }
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    req = urllib.request.Request(url, data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"[push] sent ({r.status}) to {NTFY_SERVER}/{NTFY_TOPIC}")


def ping_healthcheck(suffix=""):
    """Best-effort dead-man's switch ping (healthchecks.io etc.)."""
    if not HEALTHCHECK_URL:
        return
    try:
        urllib.request.urlopen(HEALTHCHECK_URL + suffix, timeout=10).read()
    except Exception as e:  # noqa: BLE001 - watchdog ping is best-effort
        print(f"[warn] healthcheck ping failed: {e}")


def run_check():
    url = api_url()
    print("[info]", now_utc().isoformat(), "GET", url)
    data = http_get_json(url)
    items = data if isinstance(data, list) else (data.get("data", []) or [])
    now_sigs = sorted({signature(i) for i in items})

    state = load_state()
    prev_sigs = set(state.get("signatures", []))
    new_sigs = [s for s in now_sigs if s not in prev_sigs]
    print(f"[info] available now: {len(items)} | new vs last run: {len(new_sigs)}")

    if items and new_sigs:
        title = f"Rendez-vous dispo! ({len(items)})"
        message = (f"{len(new_sigs)} nouveau(x) creneau(x) a Clic Sante.\n"
                   f"{human_summary(items)}\n\nReserver tout de suite ->")
        send_push(title, message)

    state["signatures"] = now_sigs
    state["count"] = len(items)
    if items:
        state["last_seen"] = now_utc().isoformat()
    state["last_check"] = now_utc().isoformat()
    state["last_status"] = "ok"
    state.pop("last_error", None)
    state["heartbeat"] = dt.date.today().isoformat()  # daily commit -> keeps cron alive
    save_state(state)
    return state


def main():
    if "--test-push" in sys.argv:
        send_push("Clic Sante monitor: test",
                  "If you can read this on your phone, notifications work.",
                  tags="white_check_mark")
        return 0

    if "--selftest" in sys.argv:
        # End-to-end validation of detect->notify with fake slots (clearly labelled).
        sample = [{"id": "SELFTEST-1", "date": "2026-06-15", "startTime": "09:30"},
                  {"id": "SELFTEST-2", "date": "2026-06-16", "startTime": "14:00"}]
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
                elapsed = (now_utc() - dt.datetime.fromisoformat(last)).total_seconds()
                throttled = elapsed < ERROR_PUSH_THROTTLE_MIN * 60
            except ValueError:
                throttled = False
        if not throttled:
            send_push("Clic Sante monitor ERROR",
                      f"The watcher failed to check availabilities:\n{msg[:300]}\n"
                      "It will keep retrying. Check the GitHub Actions logs.",
                      tags="warning", priority="default")
            st["last_error_push"] = now_utc().isoformat()
        st["last_check"] = now_utc().isoformat()
        st["last_status"] = "error"
        st["last_error"] = msg[:500]
        save_state(st)
        ping_healthcheck("/fail")
        raise

    ping_healthcheck()  # signal "I ran successfully" to the external watchdog

    if HEARTBEAT_PUSH and state.get("heartbeat_push_date") != dt.date.today().isoformat():
        send_push("Clic Sante monitor OK",
                  f"Still watching. {state.get('count', 0)} dispo right now.",
                  tags="white_check_mark", priority="min")
        state["heartbeat_push_date"] = dt.date.today().isoformat()
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
