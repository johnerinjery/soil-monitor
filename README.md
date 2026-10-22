# Soil and environment monitor

An ESP32 node in a plant pot measures soil moisture (3 probes), soil temperature, air temperature, humidity and light
every 15 minutes and sends the readings to Supabase. A scheduled GitHub Actions job turns the raw rows into derived
indicators and publishes a static dashboard on GitHub Pages.

```
ESP32 node ──HTTPS POST──▶ Supabase table `readings`
                               │  (secret key, only inside GitHub Actions)
                               ▼
                    GitHub Actions, every ~30 min
                    python -m soilmon.run  →  latest/series/raw/daily/events.json
                               │
                               ▼
                    GitHub Pages: static page + JSON snapshot
                    (browser loads files only: no keys, no database calls)
```

## Repository layout

| Path | What it is |
|---|---|
| `firmware/soil_node/` | The Arduino sketch (`soil_node.ino`) and `secrets.example.h`. Copy the latter to `secrets.h` (git-ignored). |
| `supabase/setup.sql` | Table and grants. |
| `config/calibration.yaml` | Probe anchors, divider values, light factor, timezone. **You edit this.** |
| `config/thresholds.yaml` | Every threshold and tuning number. |
| `config/manual_events.csv` | Your watering log (optional but valuable). |
| `inference/soilmon/` | The Python package: `clean`, `derive`, `events`, `forecast`, `indicators`, `status`, `export`, `fetch`, `simulator`, `calibrate`, `run`. |
| `inference/tests/` | 84 tests, including an end-to-end run on simulated data with injected faults. |
| `web/` | The dashboard (`index.html`, `app.js`, `style.css`, vendored Plotly). `web/data/sample/` is demo data. |
| `.github/workflows/` | `tests.yml`, `publish.yml` (the scheduled job), `keepalive.yml`. |

## One-time setup

1. **Supabase.** Run `supabase/setup.sql` in the SQL editor (it adds the `select` grant the job needs).
   In *Project Settings → API Keys* copy the project URL and the **secret** key (`sb_secret_...`).
2. **GitHub repo.** Create a **public** repository (Pages on the free plan needs a public repo; this also means the
   dashboard and its data are public). Push this folder. Check that `firmware/soil_node/secrets.h` is *not* in the commit.
3. **Secrets.** *Settings → Secrets and variables → Actions → New repository secret*:
   `SUPABASE_URL` (e.g. `https://abcd.supabase.co`) and `SUPABASE_SECRET_KEY`.
4. **Pages.** *Settings → Pages → Build and deployment → Source: GitHub Actions*.
5. **First run.** *Actions → Publish dashboard → Run workflow*. When it is green, the site is at
   `https://<user>.github.io/<repo>/`. If the run fails, the log says why (a wrong key gives an HTTP 401 with a hint).
6. Flash the firmware (`CYCLE_S 900`, `DEBUG_SERIAL 0` for deployment) and leave it running.

The node keeps working if the job fails: data accumulates in Supabase and the next successful run catches up.

### What to expect

* The schedule is `7,37 * * * *`. GitHub may delay or skip scheduled runs under load, so "updated 40 min ago" is normal.
* In a public repo GitHub disables scheduled workflows after 60 days without repository activity.
  `keepalive.yml` adds an empty commit when the repo has been quiet for 45 days; if you ever see the schedule disabled,
  press *Enable workflow* on the Actions tab. (I could not test this behaviour from here, so treat it as a best effort.)
* Supabase free projects pause after about a week without API activity; the node's inserts count as activity.

## Run it locally

```bash
cd inference && pip install -r requirements-dev.txt && cd ..
python -m pytest inference/tests -q                                   # tests

export PYTHONPATH=inference
python -m soilmon.run --source sim --days 30 --out web/data           # demo data, clearly labelled "simulated"
python -m soilmon.run --source csv --csv readings.csv --out web/data  # a CSV export of the Supabase table
SUPABASE_URL=... SUPABASE_SECRET_KEY=... python -m soilmon.run --source supabase --out web/data

python -m http.server -d web 8000                                     # open http://localhost:8000
```

If `web/data/latest.json` is missing, the page falls back to `web/data/sample/` and shows a "demo data" banner.

## Calibrate the moisture probes (do this once the probes are in the pot)

The probes are resistive modules, so the raw voltage is converted to conductance, corrected to 25 °C with the soil
temperature, and mapped between a **dry** and a **wet** anchor to give an index from 0 to 1. Until you set the anchors
the dashboard shows "uncalibrated": trends, watering events and drying rates are still meaningful, absolute levels are not.

1. Let the pot dry as far as you are willing to (or hold the probes in dry air for the dry anchor). Note the local time.
2. Water thoroughly until it drains, wait 30 to 60 minutes after the dripping stops. Note the time.
3. Wait for the next upload, then:

```bash
python -m soilmon.calibrate --source supabase \
  --dry "2026-10-25 08:00,2026-10-25 10:00" --wet "2026-10-26 18:00,2026-10-26 20:00"
```

It prints the median mV per probe in each window (and warns about noisy or unplugged probes) plus a YAML snippet.
Paste it under `moisture.probes` in `config/calibration.yaml` and set `calibrated: true`.
Also check `r_fixed_ohm` (10 kΩ is typical for YL-69 / FC-28 boards; measure the fixed resistor on yours).
Optional: weigh the pot at the dry and wet states; the weight difference is the water the index spans.

**Log your waterings.** Add a line per watering to `config/manual_events.csv`
(`2026-10-26 18:05,water,250,first calibration watering`). The dashboard then reports how many it detected itself.

**Light factor.** The BH1750 reports lux, plants use PPFD. `light.ppfd_per_lux` is a blend (0.0165) for daylight plus lamps.
Once the lighting is settled, set it to 0.0185 (daylight), 0.0146 (white LED) or 0.0135 (cool-white fluorescent).
Until then treat the daily light integral as good to about ±30 %.

## What the dashboard computes

| Indicator | Method | Main assumption / limit |
|---|---|---|
| Moisture index (0–1) | mV → conductance → 25 °C → between anchors; median of valid probes, spread between probes | relative scale, not % water content |
| I1 Watering events | index rises ≥ 0.10 within 2 h in ≥ 2/3 of valid probes, persists, 3 h refractory | a very wet pot being topped up may rise less than 0.10 |
| I2 Water-soon window | Theil–Sen fit of index against cumulative VPD since the last watering; window from the fit's range (widened for autocorrelation, ≥ 10 % floor) and day-to-day VPD variation | a window, not a date; linear dry-down is an approximation; needs ≥ 12 h after a watering |
| I3 Saturated dwell | hours with index ≥ 0.95 beyond a 6 h settling time (24 h note, 72 h alert) | depends on the wet anchor |
| I4 Humid-air exposure | leaky integrator: +1 h per hour at RH ≥ 80 % (5–40 °C), −0.5 h otherwise | an exposure proxy, not a mould measurement |
| I5 VPD and drying anomaly | VPD from the Magnus formula with ±1σ from DHT22 accuracy; drying coefficient above 1.5× its own median for ≥ 6 h | DHT22 ±2–5 % RH dominates the error |
| I6 Light | PPFD = factor × lux; DLI per local day with coverage; photoperiod; hour × day heat map | source mix unknown, BH1750 saturates near 54,600 lx |
| I7 Thermal lag | one-pole model of soil temperature chasing air temperature | sun on the pot or a heater near a probe breaks it |
| I8 Data quality | per-probe states (ok, floating, open, noisy, flagged), spread, dropout, stuck DHT22, lost records, restarts, electrode drift | – |
| I9 Condensation margin | soil temperature − dew point | an indicator of mould-friendly surfaces |
| I10 Comfort | share of the last 24 h with air 18–28 °C and RH 30–70 % | defaults for general indoor plants |

All thresholds are in `config/thresholds.yaml`. Expand **Show your work** on the page to see each derivation with the latest numbers.

### What the cleaning does (and shows)

Rows without a valid timestamp, duplicates (same device, boot and sequence), and implausible timestamps are dropped and counted
on the page. A probe reading below 200 mV (a floating pin reads about 142 mV), near the supply voltage (unplugged), with
large internal scatter (loose wire) or flagged by the firmware is left out of the analysis for that row, and listed.
Isolated spikes in air temperature and humidity are filtered with a Hampel test. Nothing is hidden: the **Raw measurements**
section plots the original values and rings the ones the analysis ignored.

## Firmware changes in this version (1.0.1)

`NTP_TIMEOUT_MS` 4000 → 8000 (hotspots were slow enough to leave rows without a timestamp) and `MOIST_MV_MIN_VALID` 20 → 200
(a floating channel reads about 142 mV and was not being flagged). Everything else is as flashed.
Re-flash only if you want those two changes; the pipeline already handles both situations.

## Security notes

* The deployed site and its JSON are **public**. They contain readings only: no keys.
* `SUPABASE_SECRET_KEY` exists only as a GitHub Actions secret. Never put it in the firmware, the repo or `web/`.
* The firmware holds the publishable key and, with `TLS_VERIFY 0`, does not verify the server certificate. That is fine on a
  bench and weak in the wild. Later hardening: `TLS_VERIFY 1` with the root certificate, and RLS with the device-key policy
  in `supabase/setup.sql`.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Run fails with HTTP 401/403 | wrong `SUPABASE_SECRET_KEY`, or the `grant select ... to service_role` has not been run |
| Run fails with `must be set` | the two secrets are missing or misspelt |
| Page says "demo data" | no `data/latest.json` next to the page: the job has not succeeded yet |
| "Snapshot is N h old" | node silent, or GitHub delayed the schedule; check *Actions* |
| A probe shows "unusable" | wiring (a floating pin reads about 142 mV), or the probe is out of the soil |
| Light dose looks low | check `ppfd_per_lux`, and where the sensor faces |
| Schedule stopped after two months | repo inactivity rule: *Enable workflow* on the Actions tab |
