# Swiss Transport APIs & Configuration Reference

This skill uses a two-tier hybrid architecture so it works immediately without credentials while supporting a seamless upgrade to Switzerland's official federal mobility feeds.

---

## 1. Backend Tiers

| Capability | Default Zero-Auth Tier | Official National Tier (`OPENTRANSPORTDATA_API_KEY`) |
| :--- | :--- | :--- |
| **Point-to-point routing** | `GET https://transport.opendata.ch/v1/connections` | `POST https://api.opentransportdata.swiss/ojp20` (`OJPTripRequest`) |
| **Live delays & platforms** | `prognosis` fields (`departure`, `arrival`, `platform`, `capacity1st/2nd`) | `EstimatedTime`, `EstimatedQuay`, `Cancelled`, `Deviation` (`UseRealtimeData=full`) |
| **Ongoing disruptions DB** | `GET https://data.sbb.ch/api/explore/v2.1/catalog/datasets/rail-traffic-information/records` | `GET https://api.opentransportdata.swiss/la/siri-sx-unplanned` + `data.sbb.ch` |
| **Station departure/arrival boards** | `GET https://transport.opendata.ch/v1/stationboard` | `POST https://api.opentransportdata.swiss/ojp20` (`OJPStopEventRequest`) |
| **Station lookup** | `GET https://transport.opendata.ch/v1/locations` | `POST https://api.opentransportdata.swiss/ojp20` (`OJPLocationInformationRequest`) |

---

## 2. Enabling the Official `opentransportdata.swiss` Tier

1. Register a free developer account at https://opentransportdata.swiss and request a key in the API Manager with access to **OJP 2.0** and **SIRI-SX**.
2. Configure the key using **either** an environment variable:
   ```bash
   export OPENTRANSPORTDATA_API_KEY="your_token_here"
   ```
   **or** persist it to `~/.config/sbb-skill/config.json` (`chmod 600`):
   ```bash
   python3 skills/sbb-trip-planner/scripts/sbb.py config --set-key "your_token_here" --set-lang en
   ```
3. Verify with:
   ```bash
   python3 skills/sbb-trip-planner/scripts/sbb.py doctor
   ```

To force a specific backend for a single command, pass `--mode zero-auth` or `--mode official` (add `--no-fallback` to prevent falling back on errors).

---

## 3. Rate Limits & Local Caching

`sbb.py` caches responses in `/tmp/sbb_skill_cache_<uid>/` to protect upstream quotas and speed up multi-step planning:

- **`SIRI-SX Unplanned` (`/la/siri-sx-unplanned`)**: Rate limit is **2 requests/min** (3,000/day). Cached locally for **90 seconds**.
- **`SIRI-SX Full/Planned` (`/la/siri-sx`, enabled via `--include-planned-siri`)**: Rate limit is **10 requests/hour** and **48 requests/day**. Cached locally for **15 minutes**—only pass `--include-planned-siri` when you specifically need operator-level planned works beyond SBB's `rail-traffic-information` feed.
- **`data.sbb.ch` (`rail-traffic-information`)**: Refreshed upstream every 5 minutes; cached locally for **120 seconds**.
- **Station lookups (`locations` / `OJPLocationInformationRequest`)**: Cached locally for **1–24 hours**.

Pass `--no-cache` to bypass the cache when debugging.

---

## 4. Swiss Rail Conventions

- **Timezones:** All output times are normalized to `Europe/Zurich` (`CET` / `CEST`).
- **Bilingual stations:** Major stations near language borders carry bilingual names (`Biel/Bienne`, `Fribourg/Freiburg`, `Murten/Morat`, `Sierre/Siders`). `sbb.py` matches disruptions across both names automatically.
- **Rail replacement (`EV` / *Bahnersatz* / *Bus de remplacement*):** During engineering works or line blocks, rail legs are replaced by buses labeled `EV`. Allow extra transfer time as replacement buses typically depart from the station forecourt (*Bahnhofplatz*) rather than train platforms.
