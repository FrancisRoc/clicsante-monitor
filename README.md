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

It replicates exactly what the booking page's calendar does:

1. Resolve the URL's unified service ids (`11,289,336,354`) to this
   establishment's real service ids: `GET /v3/establishments/8154/unified/{id}/service`.
2. For the place and each resolved service, read the day schedule:
   `GET /v3/establishments/8154/schedules/day?dateStart=<today>&dateStop=<+90d>&service=<id>&places=23139&timezone=America/Toronto&gapMode=false`
   (an empty day returns HTTP 404 `nothing-for-day`, treated as "no slots").

Slots are deduplicated by id (service variants share slots), so you're pinged
once per genuinely new slot. State is stored in `state.json`.

> We deliberately do NOT use `/establishments/{id}/availabilities` — it requires
> a resource list and silently returns `[]` without one (it would never fire).

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
