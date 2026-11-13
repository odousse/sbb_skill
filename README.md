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

### Uploading to Gemini macOS App & Claude.ai (Cloud Skills)
Build clean uploadable `.zip` bundles (free of `.DS_Store` / `__MACOSX` metadata):
```bash
./package_zip.sh
```
This produces two archives in `dist/`:
1. **`dist/sbb-trip-planner-gemini.zip`** *(for **Gemini macOS app** & **gemini.google.com**)*:
   - Open **Customize → Skills → Create → Upload** and select `dist/sbb-trip-planner-gemini.zip` (Markdown-only bundle tailored for Gemini's upload validator; uses Gemini's Web Fetch fallback against the zero-auth `GET` APIs).
2. **`dist/sbb-trip-planner.zip`** *(for **Claude.ai** & **Claude Desktop** Cloud Skills)*:
   - Upload under **Settings → Capabilities → Skills**.
   - Under **Settings → Capabilities → Code execution and file creation**, enable **Allow network access** and allowlist `transport.opendata.ch`, `data.sbb.ch`, and `api.opentransportdata.swiss` (or set egress to *All domains*).

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
