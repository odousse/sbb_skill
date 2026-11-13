# SBB Trip Planner Skill (`sbb-trip-planner`)

A portable, zero-dependency AI skill for **Google Antigravity**, **Gemini CLI**, **Claude Code**, and other coding/assistant agents to plan Swiss train journeys with **SBB / CFF / FFS** using:

- **Live timetables & connections** (including `--via` routing and arrival-time deadlines)
- **Ongoing service disruptions database** (automatic corridor cross-checking across origin, destination, transfer hubs, and intermediate stops)
- **Real-time train statuses** (delay minutes, platform changes, cancellations, rail-replacement buses `EV`, transfer margin warnings, and 1st/2nd class occupancy forecasts)

---

## Architecture

Requires **only Python 3.9+ standard library** (no `pip install` or virtual environments).

| Mode | Routing & Live Delays | Ongoing Disruptions | Auth Required |
| :--- | :--- | :--- | :--- |
| **Default (Zero-Auth)** | [transport.opendata.ch](https://transport.opendata.ch) (`/v1/connections`, `/v1/stationboard`) | [data.sbb.ch](https://data.sbb.ch) (`rail-traffic-information`) | **None** |
| **Official Upgrade (Hybrid)** | [opentransportdata.swiss](https://opentransportdata.swiss) **OJP 2.0** (`OJPTripRequest`, `OJPStopEventRequest`) | **SIRI-SX** (`/la/siri-sx-unplanned`, 90s cache) + `data.sbb.ch` | Free `OPENTRANSPORTDATA_API_KEY` |

---

## Installation

### Inside this workspace (Antigravity)
`.agents/skills.json` automatically registers `skills/sbb-trip-planner` whenever this repository is open.

### Global installation (available in any project or project-less chat)
```bash
# Install into Antigravity's global skills directory (~/.gemini/config/skills/)
./install.sh

# Or install across Antigravity, Gemini CLI, Claude Code, and ~/.agents/skills:
./install.sh --all-agents
```

---

## CLI Usage

```bash
# Plan a trip (underscores avoid shell quoting)
python3 skills/sbb-trip-planner/scripts/sbb.py trip Zürich_HB Lausanne

# Plan to arrive by a specific time
python3 skills/sbb-trip-planner/scripts/sbb.py trip Genève Bern --date 2026-11-14 --time 09:00 --arrival

# Query ongoing Swiss rail disruptions
python3 skills/sbb-trip-planner/scripts/sbb.py disruptions
python3 skills/sbb-trip-planner/scripts/sbb.py disruptions --query Olten

# Live station departure/arrival board
python3 skills/sbb-trip-planner/scripts/sbb.py board Bern --limit 8
python3 skills/sbb-trip-planner/scripts/sbb.py board Zürich_HB --to Chur --limit 5

# Search Swiss station names & DIDOK/SLOID IDs
python3 skills/sbb-trip-planner/scripts/sbb.py stations Biel

# Check API connectivity & active tier
python3 skills/sbb-trip-planner/scripts/sbb.py doctor
```

---

## Optional: Upgrade to Official `opentransportdata.swiss` Feeds

1. Get a free API key (with **OJP 2.0** and **SIRI-SX** enabled) at https://opentransportdata.swiss.
2. Save it locally (`chmod 600` at `~/.config/sbb-skill/config.json`) or export `OPENTRANSPORTDATA_API_KEY`:
   ```bash
   python3 skills/sbb-trip-planner/scripts/sbb.py config --set-key "YOUR_TOKEN" --set-lang en
   ```

---

## Running Unit Tests

```bash
python3 -m unittest discover -s tests -v
```
