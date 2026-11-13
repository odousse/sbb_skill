---
name: sbb-trip-planner
description: >-
  Plan Swiss train and public transport trips with SBB using live timetables,
  real-time train delays, platform changes, station boards, and ongoing service
  disruptions. Use this skill whenever the user asks about Swiss trains, SBB/CFF/FFS
  connections, delays, disruptions (Betriebslage/Störungen/perturbations), transfer
  feasibility, or live departures/arrivals in Switzerland.
---

# SBB & Swiss Public Transport Trip Planner

Use the bundled zero-dependency CLI [sbb.py](./scripts/sbb.py) to fetch live Swiss timetables, real-time delay prognoses, platform changes, and active rail service disruptions before recommending an itinerary.

## Quick Command Reference

Run `sbb.py` with `python3` from its installed path (replace `<SKILL_DIR>` with the directory containing this `SKILL.md`). Pass multi-word station names using underscores (`Zürich_HB`, `Basel_SBB`, `Genève_Aéroport`) so commands stay quote-free and prefix-matchable:

```bash
# 1. Plan a trip (auto-checks active disruptions along the route & intermediate stops)
python3 <SKILL_DIR>/scripts/sbb.py trip Zürich_HB Lausanne --limit 4

# Plan for a specific date/time or target arrival time (Europe/Zurich timezone)
python3 <SKILL_DIR>/scripts/sbb.py trip Genève Bern --date 2026-11-14 --time 09:00 --arrival

# Route via a specific hub
python3 <SKILL_DIR>/scripts/sbb.py trip Basel_SBB Lugano --via Luzern

# 2. Check ongoing & upcoming Swiss rail disruptions
python3 <SKILL_DIR>/scripts/sbb.py disruptions
python3 <SKILL_DIR>/scripts/sbb.py disruptions --query Olten
python3 <SKILL_DIR>/scripts/sbb.py disruptions --all --query Gotthard

# 3. Check live station departure or arrival board (with real-time delays & platform changes)
python3 <SKILL_DIR>/scripts/sbb.py board Bern --limit 10
python3 <SKILL_DIR>/scripts/sbb.py board Zürich_HB --to Chur --limit 5
python3 <SKILL_DIR>/scripts/sbb.py board Lausanne --arrivals --limit 8

# 4. Disambiguate station names or look up DIDOK/SLOID IDs
python3 <SKILL_DIR>/scripts/sbb.py stations Biel

# 5. Check active backend tier & API health
python3 <SKILL_DIR>/scripts/sbb.py doctor
```

Add `--json` to any subcommand when structured JSON output is needed instead of Markdown.

---

## Trip-Planning Workflow

1. **Resolve ambiguous stops if needed:**
   - Use standard Swiss station names (`Zürich_HB`, `Bern`, `Basel_SBB`, `Genève`, `Genève-Aéroport`, `Lausanne`, `Luzern`, `Lugano`, `Biel/Bienne`, `Fribourg/Freiburg`).
   - If the user names a neighborhood, landmark, or ski resort, run `sbb.py stations <name>` first to pick the exact stop.
2. **Fetch candidate connections & corridor disruptions:**
   - Run `sbb.py trip <origin> <destination>` (with `--date`/`--time` and `--arrival` if the user must arrive by a deadline).
   - `sbb.py trip` automatically cross-references active disruptions across origin, destination, transfer hubs, and intermediate stops.
   - If planning a trip days in advance or across construction-heavy corridors, also run `sbb.py disruptions --all --query <corridor>` to catch future weekend engineering works.
3. **Evaluate real-time delays & transfer safety:**
   - Inspect every connection's delay badges (`+X min`), platform change notices (`Pl. 4 (changed from 3!)`), `[CANCELLED]` or `[RAIL REPLACEMENT BUS]` (`EV`) flags, and `⚠️` transfer warnings.
   - **Major hub rule:** At large stations (`Zürich HB`, `Bern`, `Basel SBB`, `Genève`, `Lausanne`, `Olten`), treat effective transfers under **5 minutes** (especially cross-sector changes like `Pl. 4` to `Pl. 31` in Zürich HB) as risky when the incoming train is delayed. Always present the **next backup connection** so the traveler knows their fallback.
4. **Present a clear, actionable recommendation:**
   - Lead with the recommended departure, total duration, platforms, and transfer stations/margins.
   - Explicitly call out any **active disruptions**, **platform changes**, **rail-replacement buses (`EV`)**, or **high 1st/2nd class occupancy** (`3/3`).

---

## Hybrid Backend & Optional Official API Key

`sbb.py` works out of the box with **zero configuration** (`transport.opendata.ch` + `data.sbb.ch`). If the user wants official national `OJP 2.0` disruption-aware routing and granular `SIRI-SX` incident feeds from [opentransportdata.swiss](https://opentransportdata.swiss), see [references/api-details.md](./references/api-details.md) for setup via `OPENTRANSPORTDATA_API_KEY` or `sbb.py config --set-key`.
