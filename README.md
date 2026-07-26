# openttd-telemetry

Turns OpenTTD's monthly autosaves into CSV time-series data for towns,
stations, and vehicles — population growth, cargo production, station
ratings, fleet status — without touching the running game or occupying
its single GameScript slot.

## How it works

OpenTTD writes a full `.sav` file on its own monthly autosave schedule.
This project watches that autosave folder and, each time a new save
appears, parses it externally in Python and writes out CSVs.

Two independent data sources feed the extraction, because a savegame's raw
data chunks turned out to be incapable of answering some of these questions
on their own:

- **Direct savegame parsing** (via [OpenTTDLab](https://github.com/michalc/OpenTTDLab)),
  for stations and vehicles — this data is genuinely present in the save
  and reads out cleanly.
- **RVG Telemetry** ([`rvg_fork/`](rvg_fork/), a submodule) — a fork of the
  [Renewed Village Growth](https://github.com/F1rrel/RenewedVillageGrowth)
  (RVG) GameScript, for towns. The raw savegame has no population field at
  all, and town/station names are blank unless a player manually renamed
  them (the real display name is normally synthesized by the game client,
  not stored). RVG's GameScript API access resolves the real values
  instead (`GSTown.GetPopulation()`, `GetName()`, etc.), snapshots them
  once a month into its own save data, and this project decodes that
  snapshot straight out of the savegame's `GSDT` chunk. It runs as its own
  distinct GameScript ("RVG Telemetry", short name `RVGT`) — same growth
  logic as upstream RVG, selected separately rather than replacing it, so
  it can't be confused with or interfere with an existing RVG install.

See [CLAUDE.MD](CLAUDE.MD) for the full technical trail — why the raw
chunks fall short, how the `GSDT` decoder works, and the repo/submodule
layout.

## Setup

```
pip install OpenTTDLab Pillow
```
(`pandas` isn't a dependency of the script itself — the CSV aggregation
below is done with the stdlib `csv` module — but it's the natural tool for
actually analyzing the resulting `towns.csv`/`stations.csv`/`vehicles.csv`
once you have them.)

In-game:
1. Make `rvg_fork/` discoverable by OpenTTD: it needs to live under
   `<OpenTTD user dir>/game/` (loose files — the same place any locally
   developed GameScript goes, separate from BaNaNaS-managed content).
   `<OpenTTD user dir>` is `~/Documents/OpenTTD` on both Windows and
   macOS. Link it in rather than copying, so edits to `export.nut`/
   `main.nut` are live in-game immediately:
   - Windows (a directory junction works without admin rights):
     ```
     cmd /c mklink /J "<OpenTTD user dir>\game\rvg_fork" "<this repo>\rvg_fork"
     ```
   - macOS (a plain symlink — no admin-rights wrinkle here):
     ```
     ln -s "<this repo>/rvg_fork" "<OpenTTD user dir>/game/rvg_fork"
     ```
2. Start a new game, open **AI/Game Script Settings**, and select
   **"RVG Telemetry"** from the Game Script dropdown — *not* "Renewed
   Village Growth" (the unmodified original, if it's also installed) and
   not any other GameScript folder that might exist under `game/`.
3. Set the autosave interval to monthly (Settings → Environment → Autosave,
   or the `autosave` value in `openttd.cfg`).
4. Let at least one in-game month pass, then confirm it's working with
   `--dump-rvg-export` (see Usage below) before relying on the watch loop.

## Usage

The script itself is OS-agnostic — pure Python/`pathlib`, no Windows-only
APIs — and `~/Documents/OpenTTD` (the default personal directory it looks
for `screenshot`/`scripts` under) is correct on macOS as well as Windows
with no changes needed. The one thing that differs per platform is where
the OpenTTD executable itself lives, via `--openttd-exe`:

- **Windows**: defaults to `C:\Program Files\OpenTTD\openttd.exe` (no
  need to pass `--openttd-exe` unless installed elsewhere). A
  double-click launcher is also available — see `watch_autosaves.bat`
  below.
- **macOS**: pass `--openttd-exe`, pointing at the binary inside the app
  bundle, typically:
  ```
  --openttd-exe "/Applications/OpenTTD.app/Contents/MacOS/openttd"
  ```
  There's no dedicated launcher script for macOS — the command below is
  the whole thing.

Watch an autosave folder and write CSVs (plus a full-map screenshot) for
every new save as it appears:
```
python openttd_telemetry.py --watch-dir "/path/to/autosave" --out-dir "./extracted_data" [--no-screenshots] [--openttd-exe "<path-to-openttd-executable>"]
```
Screenshots work by briefly launching OpenTTD itself against each save
non-interactively (there's no way to render one from the savegame data
directly) — expect a real OpenTTD window to flash on screen for a few
seconds per save. Pass `--no-screenshots` to skip this and only write CSVs.

[`watch_autosaves.bat`](watch_autosaves.bat) wraps the above for the
standard OpenTTD autosave location — double-click it (or run it from a
terminal) to start watching with sensible defaults, no arguments needed.

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

Each processed `.sav` file appends to three persistent, growing CSVs in
`--out-dir` — `towns.csv`, `stations.csv`, `vehicles.csv` — plus writes one
per-save map screenshot named after the save's own stem (e.g.
`autosave3_map.jpg`). The three CSVs are shared across every save ever
processed into that `--out-dir` (this *is* the time-series aggregation —
there's no separate later step to stitch per-save files together, since
each save's rows just land directly in the combined file). Every row
carries a `save` column identifying which save it came from — this is
what makes the accumulated data a time series rather than just a snapshot.
Re-processing the same save twice would duplicate its rows; in normal use
`watch_and_process`'s `.processed.json` tracking prevents that.

**`towns.csv`** — one row per town per processed save:

| column | source | notes |
| --- | --- | --- |
| `save` | save file stem | identifies which save this row came from |
| `town_id` | savegame | |
| `name` | RVG export (falls back to savegame) | savegame's own name is blank unless manually renamed |
| `population` | RVG export only | not present in the raw savegame at all |
| `houses` | RVG export only | |
| `passengers_produced` | RVG export only | last economy-month, resolved via cargo class, not a hardcoded cargo index |
| `mail_produced` | RVG export only | same |

RVG-sourced columns are blank until the modified script has completed at
least one in-game monthly tick on that save.

**`stations.csv`** — one row per (non-waypoint) station per processed save:

| column | notes |
| --- | --- |
| `save` | identifies which save this row came from |
| `station_id` | |
| `name` | blank unless manually renamed, same caveat as town names |
| `town_id` | owning town |
| `owner` | |
| `facilities` | bitmask of station facility types |
| `num_cargo_types_with_goods_data` | count only |
| `goods_raw` | raw per-cargo-type rating/waiting data as JSON; cargo type here is positional, not yet mapped to a cargo name |

**`vehicles.csv`** — one row per train/road vehicle/ship/aircraft per
processed save (depot-only "effect"/disaster entries are skipped):

| column | notes |
| --- | --- |
| `save` | identifies which save this row came from |
| `vehicle_id` | |
| `type` | `train` / `roadveh` / `ship` / `aircraft` |
| `name` | blank unless manually named |
| `owner` | |
| `cargo_type` | positional cargo index, same caveat as station goods |
| `cargo_cap` | |
| `profit_this_year` | |
| `age` | |

**`<stem>_map.jpg`** — a full-map screenshot, rendered by briefly
launching OpenTTD itself against that save (skip with `--no-screenshots`).
Captured at native "giant" resolution then downscaled to fit within
`SCREENSHOT_MAX_DIMENSION` (3840px longest side) and re-encoded as JPEG
(`SCREENSHOT_JPEG_QUALITY` = 90) — chosen after measuring a real save's
output: ~1MB/frame with no visible quality loss, versus ~75MB for the
untouched original. These stay one file per save (not consolidated like
the CSVs), since that's what a time-lapse video needs — one frame in, one
frame out.

## Known limitations

- Map screenshots briefly write into OpenTTD's real `scripts/game_start.scr`
  during capture (deleted immediately after) rather than a sandboxed copy —
  a sandboxed `-X` run was tried first but silently fails to load any save
  at all, since it also cuts off this game's NewGRF dependencies. If an
  interactive OpenTTD session happens to start/load a game at the exact
  moment a headless screenshot run is mid-flight, that session would also
  get screenshotted-and-quit — a narrow, few-second window per save.
- Cargo type in the stations/vehicles CSVs is a raw positional index, not
  a resolved name — this game runs NewGRF industry sets that reassign
  cargo IDs per-game, so a fixed index-to-name mapping isn't safe. The
  same `GSCargo`-based resolution RVG uses for passengers/mail could be
  extended to stations if this becomes needed.
