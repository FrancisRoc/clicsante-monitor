# clicsante-monitor

Watch **any** [Clic Santé](https://clicsante.ca) booking page for new appointment
availabilities and get an instant **phone push** the moment a slot opens up.

- Works for **any clinic / any link** — just paste your Clic Santé URL.
- Runs **24/7 in the cloud** (GitHub Actions) — keeps working while your computer
  is asleep, closed, or off.
- **Free**: a public repo gets unlimited Actions minutes.
- No servers, no account besides GitHub + a free push app.

It reads the **same calendar endpoint the booking page uses**, and alerts only on
days that are genuinely **bookable** (not days that exist but are full), so a
notification means "the page would let you book that day".

---

## Set it up (about 5 minutes, no coding)

### 1. Make your own copy
Click **“Use this template” → Create a new repository**. Make it **Public**
(public repos get unlimited free Actions minutes). It only contains this monitor
code — nothing personal.

### 2. Pick how you’ll be notified (ntfy)
Notifications use [ntfy](https://ntfy.sh) — free, no account.
1. Install the ntfy app ([iOS](https://apps.apple.com/app/ntfy/id1625396347) /
   [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)).
2. Choose a **unique topic name** that others won’t guess, e.g.
   `clicsante-rdv-marie-8471`. (Anyone who knows it can read/send notifications,
   so make it unique.)
3. In the app: **Subscribe → topic** = your name, server `ntfy.sh`.

### 3. Tell the monitor your link + topic
In your new repo: **Settings → Secrets and variables → Actions**.
- **Variables** tab → **New repository variable**:
  `CS_URL` = your full Clic Santé booking link (the one in your browser’s address
  bar on the booking page), e.g.
  `https://clients3.clicsante.ca/65760/take-appt?portalPlace=25141&portalServicesUnified=11,289,336,354&portalEst=466471`
- **Secrets** tab → **New repository secret**:
  `NTFY_TOPIC` = your topic name from step 2.

### 4. Turn on Actions
Open the **Actions** tab and enable workflows if prompted.

### 5. Confirm + start
1. **Actions → clicsante-monitor → Run workflow**, tick **setup**, Run. Within a
   minute you’ll get a push: *“now watching: <clinic> … N open days right now”*.
   That proves your link and notifications are wired correctly.
2. **Run workflow** again with **nothing ticked** → this starts the always-on
   monitor. Done — leave it. You’ll get a push whenever a new bookable day opens.

That’s it. The monitor keeps itself running 24/7 from here on.

---

## Everyday use

- **See your notification history:** open `https://ntfy.sh/<your-topic>` in a
  browser, or the ntfy app.
- **Change which clinic/link:** edit the `CS_URL` variable. The change is picked
  up automatically within ~5 hours (or run the workflow once to apply now).
- **Pause:** Actions tab → ••• → *Disable workflow*. **Resume:** re-enable, then
  Run workflow once (nothing ticked) to restart the loop.
- **Stop for good:** delete the repo.
- **Test a push any time:** Run workflow with **selftest** ticked.

---

## Optional settings

Set these as repository **Variables** (or Secrets where noted) to override
defaults — all optional:

| Name | What it does | Default |
|---|---|---|
| `CS_URL` | The booking link to watch (paste your URL) | clinic 8154 example |
| `NTFY_TOPIC` *(secret)* | Your push topic | — (required for pushes) |
| `CS_LOOP_INTERVAL` | Seconds between checks | `600` (10 min) |
| `CS_LOOKAHEAD_DAYS` | How far ahead to watch | `90` |
| `NTFY_SERVER` | Custom ntfy server | `https://ntfy.sh` |
| `NTFY_TOKEN` *(secret)* | Token if your topic is access-protected | — |
| `HEARTBEAT_PUSH` | `1` = a daily “still alive” push | off |
| `HEALTHCHECK_URL` *(secret)* | Dead-man’s-switch ping URL (e.g. healthchecks.io) | off |

Instead of `CS_URL` you can set ids directly: `CS_ESTABLISHMENT`, `CS_PLACE`
(or `CS_PLACES` comma-separated), `CS_SERVICES`.

---

## How it works (for the curious)

1. **Parse the link** → establishment, place(s), and unified service ids.
2. **Resolve services**: unified ids → the establishment’s real service ids
   (`/{est}/unified/{id}/service`); if the link has no services, watch all of
   `/{est}/services`.
3. **Read the calendar** the page itself uses:
   `/{est}/schedules/public?...&service=<id>&places=<id>&timezone=America/Toronto`.
   It returns `availabilities` (bookable days) and `daysComplete` (full days);
   we alert **only on `availabilities`**, deduplicated by `(place, date)`.
4. **Notify** via ntfy, with a tap-through link that opens your exact booking page
   and a few clock times (best-effort, from `/{est}/schedules/day`).

**Reliability:** GitHub’s `schedule` cron is unreliable, so the real work is a
long-lived job that loops in-process and **relaunches itself** before GitHub’s
6 h job limit — one monitor runs continuously. A sparse hourly cron is only a
watchdog that restarts the loop if that chain ever breaks. If everything ever
stops, run the workflow once manually to re-seed it.

Notes: the 2 screening questions on the booking page are a UI gate only and don’t
change which days are returned (verified live). We avoid `/availabilities` (needs
a resource list, silently returns `[]`).

---

## Run / test locally

```bash
# validate your link + see current availability (no push without NTFY_TOPIC)
CS_URL="<your link>" python3 check_availability.py --setup

# one real check now
CS_URL="<your link>" NTFY_TOPIC="<your topic>" python3 check_availability.py

# send yourself a test push
NTFY_TOPIC="<your topic>" python3 check_availability.py --test-push
```

Pure Python 3 standard library — no dependencies to install.
