#!/usr/bin/env python3
"""
Clic Sante availability monitor.

Polls the Clic Sante availabilities API for a specific establishment/place/services
and sends a phone push (via ntfy) the moment NEW availabilities appear.

State is kept in a small JSON file so we only alert on genuinely new slots,
never on repeats. Designed to run headless on a schedule (GitHub Actions).
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
import datetime as dt

# ---- What to watch (from the booking URL the user provided) ----
ESTABLISHMENT = os.environ.get("CS_ESTABLISHMENT", "8154")
PLACE         = os.environ.get("CS_PLACE", "23139")
SERVICES      = os.environ.get("CS_SERVICES", "11,289,336,354")
TIMEZONE      = os.environ.get("CS_TIMEZONE", "America/Toronto")
LOOKAHEAD_DAYS = int(os.environ.get("CS_LOOKAHEAD_DAYS", "90"))

BOOKING_URL = os.environ.get(
    "CS_BOOKING_URL",
    "https://clients3.clicsante.ca/8154/take-appt?portalPlace=23139&portalPostalCode=null"
    "&lang=fr&portalServicesUnified=11,289,336,354&portalEst=408574&locale=fr",
)

# ---- Notification (ntfy) ----
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "")          # required to actually send
NTFY_TOKEN  = os.environ.get("NTFY_TOKEN", "")          # optional (for protected topics)

STATE_FILE = os.environ.get("CS_STATE_FILE", "state.json")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"


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
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - transient network/HTTP errors -> retry
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {last}")


def signature(item):
    """Stable identity for an availability so we can detect what's new."""
    if isinstance(item, dict):
        for key in ("id", "availabilityId", "uuid"):
            if item.get(key) is not None:
                return f"{key}:{item[key]}"
        return json.dumps(item, sort_keys=True, ensure_ascii=False)
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


def human_summary(items, limit=10):
    """Best-effort: pull date/time-ish fields for a readable message."""
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
    # NOTE: ntfy header values must be ASCII, and the "Actions" shorthand is
    # comma-delimited -- the booking URL contains commas, so we rely on the
    # tappable "Click" header instead (opens the booking page on tap).
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


def main():
    dry = "--test-push" in sys.argv
    if dry:
        send_push("Clic Sante monitor: test",
                  "If you can read this on your phone, notifications work. "
                  "You'll get a push here when appointments open up.",
                  tags="white_check_mark")
        return 0

    url = api_url()
    print("[info]", dt.datetime.now().isoformat(), "GET", url)
    data = http_get_json(url)
    items = data if isinstance(data, list) else data.get("data", []) or []
    now_sigs = sorted({signature(i) for i in items})

    state = load_state()
    prev_sigs = set(state.get("signatures", []))
    new_sigs = [s for s in now_sigs if s not in prev_sigs]

    print(f"[info] available now: {len(items)} | new vs last run: {len(new_sigs)}")

    if items and new_sigs:
        summary = human_summary(items)
        title = f"Rendez-vous dispo! ({len(items)})"
        message = (f"{len(new_sigs)} nouveau(x) créneau(x) à Clic Santé.\n"
                   f"{summary}\n\nRéserver tout de suite →")
        send_push(title, message)

    state["signatures"] = now_sigs
    state["count"] = len(items)
    state["last_seen"] = dt.datetime.now(dt.timezone.utc).isoformat() if items else state.get("last_seen")
    state["last_check"] = dt.datetime.now(dt.timezone.utc).isoformat()
    # Date-only heartbeat: makes state.json change once per day so the repo gets
    # a daily commit, keeping the scheduled workflow from being auto-disabled
    # after 60 days of inactivity (without committing on every 5-min run).
    state["heartbeat"] = dt.date.today().isoformat()
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
