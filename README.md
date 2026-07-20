# openttd-telemetry

Turns OpenTTD's monthly autosaves into CSV time-series data for towns,
stations, and vehicles — population growth, cargo production, station
ratings, fleet status — without touching the running game or occupying
its single GameScript slot.

## How it works

OpenTTD writes a full `.sav` file on its own monthly autosave schedule.
This project watches that autosave folder and, each time a new save
appears, parses it externally in Python and writes out CSVs. Nothing runs
inside the game itself except a locally modified copy of the
[Renewed Village Growth](https://github.com/F1rrel/RenewedVillageGrowth)
(RVG) GameScript this game already runs — used only to *read* real town
data, not to change how towns grow.

Two independent data sources feed the extraction, because a savegame's raw
data chunks turned out to be incapable of answering some of these questions
on their own:

- **Direct savegame parsing** (via [OpenTTDLab](https://github.com/michalc/OpenTTDLab)),
  for stations and vehicles — this data is genuinely present in the save
  and reads out cleanly.
- **A modified RVG fork** ([`rvg_fork/`](rvg_fork/), a submodule), for
  towns — the raw savegame has no population field at all, and town/station
  names are blank unless a player manually renamed them (the real display
  name is normally synthesized by the game client, not stored). RVG's
  GameScript API access resolves the real values instead
  (`GSTown.GetPopulation()`, `GetName()`, etc.), snapshots them once a
  month into its own save data, and this project decodes that snapshot
  straight out of the savegame's `GSDT` chunk.

See [CLAUDE.MD](CLAUDE.MD) for the full technical trail — why the raw
chunks fall short, how the `GSDT` decoder works, and the repo/submodule
layout.

## Setup

```
pip install OpenTTDLab pandas
```

In-game:
1. Point OpenTTD's AI/GameScript folder at `rvg_fork/` (on its
   `export-data` branch) instead of whatever copy of RVG it currently
   loads — see [CLAUDE.MD](CLAUDE.MD)'s "Repository setup" section for how
   `rvg_fork/` is wired up as a submodule.
2. Set the autosave interval to monthly (Settings → Environment → Autosave,
   or the `autosave` value in `openttd.cfg`).

## Usage

Watch an autosave folder and write CSVs for every new save as it appears:
```
python openttd_telemetry.py --watch-dir "/path/to/autosave" --out-dir "./extracted_data"
```

Inspect a savegame's raw chunk structure directly — useful when adding new
fields or debugging extraction logic:
```
python openttd_telemetry.py --inspect "/path/to/some/autosave.sav" [--chunk CITY] [--sample-count 5]
```

Decode and print just the RVG export data (bypasses the CSV pipeline
entirely) — useful for confirming the modified script is actually
producing data:
```
python openttd_telemetry.py --dump-rvg-export "/path/to/some/autosave.sav"
```

## Output

Each processed `.sav` file produces three CSVs in `--out-dir`, named after
the save file's own stem (e.g. `autosave3_towns.csv`), so every autosave
becomes one labeled snapshot in time. There is no in-place aggregation yet
— each run adds a new set of per-save files rather than appending to a
combined table (a `pandas`-based step to stitch these into a single
time-series dataframe is planned but not built).

**`<stem>_towns.csv`** — one row per town:

| column | source | notes |
| --- | --- | --- |
| `town_id` | savegame | |
| `name` | RVG export (falls back to savegame) | savegame's own name is blank unless manually renamed |
| `population` | RVG export only | not present in the raw savegame at all |
| `houses` | RVG export only | |
| `passengers_produced` | RVG export only | last economy-month, resolved via cargo class, not a hardcoded cargo index |
| `mail_produced` | RVG export only | same |

RVG-sourced columns are blank until the modified script has completed at
least one in-game monthly tick on that save.

**`<stem>_stations.csv`** — one row per (non-waypoint) station:

| column | notes |
| --- | --- |
| `station_id` | |
| `name` | blank unless manually renamed, same caveat as town names |
| `town_id` | owning town |
| `owner` | |
| `facilities` | bitmask of station facility types |
| `num_cargo_types_with_goods_data` | count only |
| `goods_raw` | raw per-cargo-type rating/waiting data as JSON; cargo type here is positional, not yet mapped to a cargo name |

**`<stem>_vehicles.csv`** — one row per train/road vehicle/ship/aircraft
(depot-only "effect"/disaster entries are skipped):

| column | notes |
| --- | --- |
| `vehicle_id` | |
| `type` | `train` / `roadveh` / `ship` / `aircraft` |
| `name` | blank unless manually named |
| `owner` | |
| `cargo_type` | positional cargo index, same caveat as station goods |
| `cargo_cap` | |
| `profit_this_year` | |
| `age` | |

## Known limitations

- Cargo type in the stations/vehicles CSVs is a raw positional index, not
  a resolved name — this game runs NewGRF industry sets that reassign
  cargo IDs per-game, so a fixed index-to-name mapping isn't safe. The
  same `GSCargo`-based resolution RVG uses for passengers/mail could be
  extended to stations if this becomes needed.
- No aggregation step yet combines the per-save CSVs into one time-series
  dataframe.
