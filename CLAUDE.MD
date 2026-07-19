# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Extracts town (name, population, houses, passenger/mail production), station
(name, town, cargo/rating info), and vehicle (name, type, status) data from
OpenTTD savegames once per in-game month. Approach: rely on OpenTTD's own
autosave (set to monthly in-game) and parse the resulting `.sav` files
externally with Python — this stays fully decoupled from the live game and
avoids occupying the single GameScript slot (which runs a locally-modified
fork of Renewed Village Growth / RVG, see `rvg_fork/` below).

Two independent data sources feed the extraction, because raw savegame
chunks turned out to be structurally incapable of providing real population,
real (non-procedural) names, or reliable cargo-type identification — see
"Why two data sources" below:
- **`openttd_telemetry.py`** parses `STNN` (stations) and `VEHS`
  (vehicles) directly from the savegame via OpenTTDLab — this data is
  genuinely present and works.
- **`rvg_fork/`** is a locally-maintained fork of the RVG GameScript
  (Squirrel) that snapshots real per-town data (via the GameScript API,
  which resolves values the raw savegame doesn't expose) into its own
  `Save()` table every in-game month. `openttd_telemetry.py` decodes that
  data straight out of the savegame's `GSDT` chunk with a small
  hand-written binary parser (`extract_rvg_export_table`), since OpenTTDLab
  doesn't support this format at all.

There is no build step or test suite; verification is done by running
`--inspect`/`--dump-rvg-export` against real autosaves.

## Environment

- Windows, Python via the python.org installer — verify with `python --version`.
- Dependencies: `OpenTTDLab`, `pandas` — verify with `pip show OpenTTDLab pandas`.
  (`pandas` is a stated dependency but not yet imported/used in the script;
  the intended use is a later per-month CSV aggregation step, not yet started.)
- No virtualenv or requirements file exists in this repo — install packages
  as needed with `pip install OpenTTDLab pandas`.
- Parsing is done via **OpenTTDLab** (PyPI), built on Patric Stout's OpenTTD
  Savegame Reader. Source of truth for the parser's actual behavior:
  https://github.com/michalc/OpenTTDLab/blob/main/openttdlab.py — check
  there first if `parse_savegame()`'s output shape ever looks wrong, rather
  than guessing. Its README cautions that it does not support running
  experiments using OpenTTD 14.*; `parse_savegame` itself is expected to
  work more broadly but hasn't been explicitly stress-tested against
  whatever OpenTTD version produced the local autosaves.

## Repository setup

This is two GitHub repos wired together via a git submodule — cloning
without `--recurse-submodules` leaves `rvg_fork/` as an empty directory:
```
git clone --recurse-submodules https://github.com/hpendurkar/openttd-telemetry.git
# or, if already cloned without that flag:
git submodule update --init
```

- **`hpendurkar/openttd-telemetry`** (private) — this repo:
  `openttd_telemetry.py`, `CLAUDE.MD`, `.gitignore`, and the `rvg_fork`
  submodule reference.
- **`hpendurkar/RenewedVillageGrowth`** (private) — a real GitHub fork of
  https://github.com/F1rrel/RenewedVillageGrowth, checked out at
  `rvg_fork/`. Two branches matter:
  - `master` — kept as an untouched mirror of upstream (`git remote` there
    has `origin` = our fork, `upstream` = F1rrel's original), so future RVG
    releases can be pulled with `git fetch upstream && git merge
    upstream/master` without fighting our own commits.
  - `export-data` — the fork's default branch, and what the submodule
    tracks and what's checked out locally. Has exactly one commit on top of
    `master`: `export.nut` + the `main.nut` wiring described below. Point
    OpenTTD's AI/GS folder at `rvg_fork/` (on this branch) to actually run
    the modified script in-game.

## Commands

Inspect a savegame's real chunk structure — by default, one sample record
from every chunk present (there are ~60); use `--chunk`/`--sample-count` to
narrow it down. Do this before trusting/changing any extraction logic:
```
python openttd_telemetry.py --inspect "<path-to-autosave.sav>" [--chunk CITY] [--sample-count 5]
```

Decode and print `rvg_fork`'s exported per-town data directly (bypasses
`STNN`/`VEHS`/`CITY` entirely) — useful for verifying the GSDT decoder in
isolation:
```
python openttd_telemetry.py --dump-rvg-export "<path-to-autosave.sav>"
```

Run the watch loop, polling a directory for new autosaves and writing one set
of CSVs per save:
```
python openttd_telemetry.py --watch-dir "<path-to-autosave-dir>" --out-dir "./extracted_data" [--poll-seconds 30]
```

## Architecture

`parse_savegame()` returns:
```python
{
    'savegame_version': int,
    'chunks': {
        'TAG': {
            'headers': {...},   # schema: field types/names, NOT data
            'records': {'0': {...decoded record...}, '1': {...}, ...}
        },
        ...
    }
}
```
Always read `chunk_data['records']` for actual data — `['headers']` is
schema only. An earlier version of this script read `headers` by mistake,
which looked like a library/compatibility bug but wasn't; this is the single
most important gotcha in the codebase.

**Chunk IDs in use** (`openttd_telemetry.py:39-41`):
- `CITY` = Towns (**not** `CHTS`, which is the Cheats chunk — an earlier,
  corrected mistake; `report.log` is a capture of that earlier wrong run)
- `STNN` = Stations (normal, non-waypoint)
- `VEHS` = Vehicles
- `PLYR` = Companies, used only as a diagnostic reference chunk (it's the
  chunk shown working in OpenTTDLab's own README example)

**Nested STRUCT gotcha**: fields defined as `STRUCT` with a "has length" flag
decode as *lists* of sub-records (usually length 0 or 1), not flat dicts —
e.g. a station's `normal` field (empty list if the record is actually a
waypoint), and within it `base` (name/town/owner/facilities) and `goods`
(per-cargo-type ratings). A vehicle record has `train`/`roadveh`/`ship`/
`aircraft` fields where only one is ever populated (as a single-item list),
each holding a nested `common` struct (name/owner/cargo_type/cargo_cap/
profit_this_year/age). The `_first()` helper (`openttd_telemetry.py:89`)
unwraps these safely.

**Pipeline**: `parse_sav_file` → one `extract_*` function per chunk → rows
of flat dicts → `write_csv`. `process_one_save` also calls
`extract_rvg_export_table` (wrapped in try/except — a decode failure there
degrades `extract_towns` to blank population/houses/etc. rather than
crashing the whole run, since `STNN`/`VEHS` extraction is independently
solid and shouldn't be taken down by the more fragile GSDT decoder) and
merges its per-town data into `extract_towns`'s rows. It names all output
CSVs after the save file's stem. `watch_and_process` is a simple polling
loop (no `watchdog` dependency despite the module docstring mentioning it)
that tracks already-processed filenames in `<out-dir>/.processed.json` so
re-running is idempotent.

## Why two data sources (raw savegame chunks turned out insufficient alone)

Exhaustive investigation (see `report3.log`/`report4.log`/`report6.log` for
the evidence trail) found the `CITY` (town) and `STNN` (station) chunks
structurally can't answer the questions this project needs:
- **No population field exists anywhere in `CITY`** — confirmed from a full
  real decoded record (`report3.log:69-131`), not just the header schema.
- **`name` is blank for any procedurally-named town or station** — both
  `CITY.name` and `STNN.normal.base.name` are empty unless a player
  manually renamed that town/station (`report3.log:74`, `report4.log:685`).
  The real display name is synthesized client-side from name-parts +
  string tables that aren't in the save.
- **Cargo type is identified only by list position** in `STNN.normal.goods`
  / `CITY.supplied`, and this game runs NewGRF industry sets (FIRS/ECS/
  YETI, per RVG's readme) that reassign cargo IDs per-game — so even
  "index 0 = passengers" isn't a safe assumption here.
- The raw per-tile map chunks (`MAPT`, `MAP5`, `MAP7`, `MAP8`, `M3LO`,
  `M3HI`, etc.) that would be needed to count houses directly are reported
  by OpenTTDLab as `{'unsupported': ''}` — it deliberately skips this
  legacy fixed-layout array format (`openttdlab.py:999-1011`) and only
  decodes the newer self-describing "table" chunk format.

`STNN`/`VEHS` structural fields (ratings, cargo waiting, cargo_cap, age,
etc.) are still read directly from the savegame — only the fields above
were the blocker.

## `rvg_fork/` — locally modified GameScript, the real fix for the above

`rvg_fork/` is a submodule checkout of our fork of the actual GameScript
this game runs (see "Repository setup" above for the fork/branch details),
with one isolated addition: `export.nut`, wired into `main.nut`. It's meant
to *replace* whatever copy of RVG OpenTTD currently loads for this game, so
point OpenTTD's AI/GS folder at `rvg_fork/` instead of the
BaNaNaS-downloaded version.

The GameScript API resolves exactly the values raw savegame chunks can't:
`GSTown.GetName()` gives the real resolved display name, `GetPopulation()`
gives real current population, `GetHouseCount()` gives house counts, and
`GSCargo.HasCargoClass(id, GSCargo.CC_PASSENGERS / CC_MAIL)` finds the
right cargo IDs dynamically regardless of which NewGRF industry set is
loaded — sidestepping the cargo-index-remapping problem entirely.

What changed, all additive and isolated from RVG's own growth logic
(deliberately kept in a separate table, `::ExportDataTable`, from RVG's own
`::TownDataTable`, so this can never affect gameplay):
- `export.nut` (new file): `ExportMonthlyTownSnapshot(town_id)` builds
  `{name, population, houses, passengers_produced, mail_produced}` for one
  town and stores it in `::ExportDataTable[town_id]`.
- `main.nut`: added `require("export.nut")`; added
  `::ExportDataTable <- {}` to the constructor; added a call to
  `ExportMonthlyTownSnapshot(town.id)` inside `MainClass::ManageTowns()`'s
  existing monthly loop (`main.nut:~486`, alongside the pre-existing
  `town.MonthlyManageTown()` call — this only runs once per real calendar
  month, matching the project's monthly-autosave cadence); added
  `save_table.export_data_table <- ::ExportDataTable` to `Save()` and a
  matching load in `Load()`.
- Only the *latest* month's snapshot is kept in-save (not a growing
  history) — the external CSV-per-autosave pipeline is what builds the
  time series, by design; the in-save table just needs to exist by the
  time each autosave happens.

**Until the modified script has run at least one monthly tick, savegames
won't have `export_data_table` yet** — `extract_towns` degrades gracefully
(blank population/houses/etc., same as before), not an error.

## The GSDT chunk and the SQSL decoder (`openttd_telemetry.py`)

`::ExportDataTable` (and RVG's own internal state) is written into the
savegame the normal way, via the GameScript's native `Save()`/`Load()`
mechanism — a real Squirrel table, serialized recursively. OpenTTDLab
cannot decode this: it only understands `GSDT`'s three fixed header fields
(`name`, `settings`, `version`) and explicitly discards everything after
them as "junk" per-record (`openttdlab.py:989-993`, the comment literally
says *"GSDT and AIPL are known chunk[s] with garbage at the end"*) — that
"junk" is exactly the data we want.

**Important dead end, worth knowing if this needs revisiting**: the first
hypothesis was that this data lives in the `AIPL` chunk. It doesn't — AIPL
is exclusively for AI *company* bots (one record per company slot; confirmed
directly from OpenTTD's own source, `src/saveload/ai_sl.cpp`), unrelated to
GameScripts entirely. The GameScript's own data is in **`GSDT`**
(`src/saveload/game_sl.cpp`'s `SaveReal_GSDT`, which writes name/settings/
version then calls `Game::Save()` — the same serialization). `GSDT` has
exactly one record (there's only one GameScript slot).

The serialization format itself (`src/script/script_instance.{cpp,hpp}`,
`ScriptInstance::SaveObject`/`SQSaveLoadType`) is a simple, fully documented
recursive tagged format:
- One presence byte before anything else: `1` = data follows, `0` = none
  (script hadn't started / crashed when saved) — easy to miss, and missing
  it desyncs every byte after.
- Then a repeating (type tag byte, payload) structure: `INT=0x00` (int64),
  `STRING=0x01` (1-byte length incl. trailing NUL, then bytes), `ARRAY=0x02`
  / `TABLE=0x03` (recursively-encoded elements, or key/value pairs for
  TABLE, terminated by `END=0xFF`), `BOOL=0x04` (1 byte), `NULL=0x05`,
  `INSTANCE=0x06` (not implemented — not needed for RVG's plain data).

`openttd_telemetry.py` implements this as a small standalone byte-walker
(`_Cursor`, `_read_table_headers`, `_decode_sqsl`, and the entry point
`extract_rvg_export_table`) because OpenTTDLab exposes no raw byte offsets
to hook into — it has to independently re-walk the compressed savegame from
scratch (decompress, then walk every chunk in file order) up to the `GSDT`
record, correctly skipping ~60 unrelated chunks along the way using their
own self-declared per-record sizes (no need to understand *their* field
layouts, just consume the right number of bytes) so the byte offset stays
in sync until `GSDT` is reached.

**Validated end-to-end against a real save** (an old autosave predating
this fork, so it only proves the decoder itself, not `export_data_table`
specifically): the decoded top-level keys
(`town_data_table`, `company_data_table`, `save_version`, `randomization`,
`use_town_sign`, `display_cargo`, `cargo_6_category`, `category_min_pop`)
matched RVG's actual `MainClass::Save()` source exactly, `town_data_table`
had exactly 79 entries matching `CITY`'s town count, and a sample entry's
fields matched `GoalTown::SavingTownData()` exactly, including a real
`max_population: 4031`. If this ever needs re-verifying, `--dump-rvg-export`
is the fastest way to check without touching the rest of the pipeline.

## Remaining known gaps

- `STNN.goods` cargo type is still positional only, unmapped to a cargo
  name, for the station extractor (not addressed by the RVG export, which
  is town-scoped) — the same `GSCargo.HasCargoClass`-style resolution
  could be added to `export.nut` per-station if this becomes needed.
- No aggregation step yet combines the per-month CSVs into a single
  time-series dataframe (pandas is installed for this purpose but unused).
- `report.log`, `report2.log`, `report3.log`, `report4.log`, `report5.log`,
  and `report6.log` are historical `--inspect` output captures from
  debugging sessions (not something to regenerate/maintain as part of
  normal work) — useful as evidence of real chunk structure when in doubt,
  but not part of the running system. **These are currently untracked and
  not pushed to the repo** (deliberately, pending a trimmed-down sample
  report + README) — so on a fresh clone, the line-number citations below
  won't resolve to an actual file yet. Chronologically: `report.log`
  predates the `CITY`/`CHTS` fix; `report2.log` predates the
  `records`-vs-`headers` fix; `report3.log` onward reflect the current,
  correct script and are trustworthy.
