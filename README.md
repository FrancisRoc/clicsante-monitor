# clicsante-monitor

Watches a specific [Clic Santé](https://clicsante.ca) booking page for new
appointment availabilities and sends an instant **phone push** (via
[ntfy](https://ntfy.sh)) the moment a slot opens up.

Runs **24/7 in the cloud** via GitHub Actions (every ~5 minutes), so it keeps
working even when your computer is asleep, closed, or off.

## What it watches

Establishment `8154`, place `23139`, services `11,289,336,354` — i.e. this URL:

```
https://clients3.clicsante.ca/8154/take-appt?portalPlace=23139&lang=fr&portalServicesUnified=11,289,336,354&portalEst=408574&locale=fr
```

It reads from the **same endpoint the booking page's calendar uses**, so the
monitor sees exactly what you'd see on the page:

1. Resolve the URL's unified service ids (`11,289,336,354`) to this
   establishment's real service ids: `GET /v3/establishments/8154/unified/{id}/service`.
2. For the place and each resolved service, read the public calendar:
   `GET /v3/establishments/8154/schedules/public?dateStart=<today>&dateStop=<+90d>&service=<id>&places=23139&timezone=America/Toronto`
   It returns two lists per service:
   - `availabilities` — days that are **open and bookable** (clickable on the calendar)
   - `daysComplete` — days that exist but are **full** (greyed out)

We alert **only on `availabilities`**, so "the monitor found a slot" means "the
page would let you book that day". Days are deduplicated by `(place, date)`, so
you're pinged once per genuinely new open day (the push enriches it with a few
clock times, best-effort, via `schedules/day`). State is stored in `state.json`.

> Verified against a live clinic: the page's 2 screening questions are a UI gate
> only — they do **not** change which days `schedules/public` returns, so the
> monitor doesn't need them.
>
> We deliberately do NOT use `/establishments/{id}/availabilities` (needs a
> resource list, silently returns `[]`) nor `schedules/day` alone (it can surface
> times on days the page marks `daysComplete`/full).

## Get notifications on your phone

Subscribe to the ntfy topic in **one** of these ways:

- **ntfy app (recommended):** install ntfy ([iOS](https://apps.apple.com/app/ntfy/id1625396347) /
  [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)),
  Subscribe → topic `clicsante-rdv-francis`, server `ntfy.sh`.
- **iPhone Safari (no app):** open `https://ntfy.sh/app`, Share → *Add to Home
  Screen*, open it from the new icon, subscribe to `clicsante-rdv-francis`, allow
  notifications. (A plain Safari tab only shows messages while open — no
  background push.)

Verify any time by opening `https://ntfy.sh/clicsante-rdv-francis` in a browser.

## Configuration

The topic is stored as a GitHub **Actions secret** so it isn't in the code:

- Secret `NTFY_TOPIC` = `clicsante-rdv-francis`  *(required)*
- Optional secret `NTFY_TOKEN` — ntfy access token if you protect the topic
- Optional repo **variables** to override defaults without editing code:
  `NTFY_SERVER`, `CS_ESTABLISHMENT`, `CS_PLACE`, `CS_SERVICES`, `CS_LOOKAHEAD_DAYS`

## Run / test locally

```bash
# send yourself a test push
NTFY_TOPIC=clicsante-rdv-francis python3 check_availability.py --test-push

# do one real check now
NTFY_TOPIC=clicsante-rdv-francis python3 check_availability.py
```

## Operating notes

- **Cadence:** GitHub's minimum cron is 5 min, and scheduled runs can be delayed
  a few minutes under load. For sub-minute polling you'd need an always-on
  machine instead of GitHub Actions.
- **Stays alive:** a once-a-day heartbeat in `state.json` produces a daily commit
  so GitHub doesn't auto-disable the schedule after 60 days of inactivity.
- **Pause it:** Actions tab → *clicsante-monitor* → ••• → *Disable workflow*
  (re-enable the same way). Or delete the repo when you've booked.
- **Change what's watched:** edit the defaults at the top of
  `check_availability.py`, or set the repo variables above.
