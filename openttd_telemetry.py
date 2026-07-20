"""
OpenTTD monthly data extractor.

Watches an autosave directory, and each time a new .sav file appears,
parses it with OpenTTDLab and extracts:
  - Towns: name, population
  - Stations: name, town, accepted/produced cargo, rating
  - Vehicles: name/type, status, current station/order info

Requires:
    pip install OpenTTDLab pandas watchdog

Setup in-game:
    Set autosave interval to monthly (Settings -> Environment -> Autosave,
    or the equivalent "autosave" config value in openttd.cfg).

Usage:
    python openttd_telemetry.py --watch-dir "/path/to/autosave" --out-dir "./extracted_data"

    Run with --inspect first against one existing .sav file to print the
    raw chunk structure, since exact field names can vary by OpenTTD/NewGRF
    version and are worth confirming before trusting the extraction logic:

    python openttd_telemetry.py --inspect "/path/to/some/autosave.sav"
"""

import argparse
import csv
import enum
import itertools
import json
import lzma
import struct
import time
import zlib
from pathlib import Path

from openttdlab import parse_savegame

# Confirmed chunk IDs from OpenTTD source (src/saveload/*.cpp handlers):
#   CITY = Towns (CORRECTED — CHTS is actually the Cheats chunk, not Towns)
#   STNN = Stations (normal, i.e. non-waypoint)
#   VEHS = Vehicles
#   GSDT = the single active GameScript's own persistent Save() data.
#          (NOT AIPL — that's exclusively for AI *company* bots, one record
#          per company slot; confirmed from OpenTTD's own ai_sl.cpp/
#          game_sl.cpp source. GSDT has exactly one record and is where
#          RVG's Save()-table actually lives.)
CHUNK_TOWNS = "CITY"
CHUNK_STATIONS = "STNN"
CHUNK_VEHICLES = "VEHS"
CHUNK_GAMESCRIPT = "GSDT"

# Name our modified RVG (rvg_fork/) reports via GetName() in info.nut — a
# distinct name/short-name (RVGT) from upstream RVG so OpenTTD never
# confuses the two, and so this lookup unambiguously finds our script's
# GSDT record regardless of whether upstream RVG is also installed.
RVG_SCRIPT_NAME = "RVG Telemetry"


class _Cursor:
    """
    Minimal forward-only big-endian byte reader over an in-memory buffer,
    reimplementing just enough of OpenTTDLab's low-level savegame primitives
    (openttdlab.py's gamma/uint*/int* readers) to walk raw chunk bytes
    ourselves. Needed because OpenTTDLab exposes no byte offsets and
    deliberately discards the data we're after (see extract_rvg_export_table).
    """

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def read(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise ValueError("Unexpected end of savegame data — byte layout assumptions may be stale.")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def u8(self) -> int:
        return self.read(1)[0]

    def i8(self) -> int:
        return struct.unpack(">b", self.read(1))[0]

    def u16(self) -> int:
        return struct.unpack(">H", self.read(2))[0]

    def i16(self) -> int:
        return struct.unpack(">h", self.read(2))[0]

    def u24(self) -> int:
        return (self.u16() << 8) | self.u8()

    def u32(self) -> int:
        return struct.unpack(">L", self.read(4))[0]

    def i32(self) -> int:
        return struct.unpack(">l", self.read(4))[0]

    def u64(self) -> int:
        return struct.unpack(">Q", self.read(8))[0]

    def i64(self) -> int:
        return struct.unpack(">q", self.read(8))[0]

    def gamma(self) -> int:
        b = self.u8()
        if (b & 0x80) == 0:
            return b & 0x7F
        if (b & 0xC0) == 0x80:
            return (b & 0x3F) << 8 | self.u8()
        if (b & 0xE0) == 0xC0:
            return (b & 0x1F) << 16 | self.u16()
        if (b & 0xF0) == 0xE0:
            return (b & 0x0F) << 24 | self.u24()
        if (b & 0xF8) == 0xF0:
            return (b & 0x07) << 32 | self.u32()
        raise ValueError("Invalid gamma encoding.")

    def gamma_str(self) -> str:
        return self.read(self.gamma()).decode()


class _FieldType(enum.IntEnum):
    """Mirrors openttdlab.py's FieldType — only need it for GSDT's flat header."""
    END = 0
    I8 = 1
    U8 = 2
    I16 = 3
    U16 = 4
    I32 = 5
    U32 = 6
    I64 = 7
    U64 = 8
    STRINGID = 9
    STRING = 10
    STRUCT = 11


_SCALAR_READERS = {
    _FieldType.I8: _Cursor.i8,
    _FieldType.U8: _Cursor.u8,
    _FieldType.I16: _Cursor.i16,
    _FieldType.U16: _Cursor.u16,
    _FieldType.I32: _Cursor.i32,
    _FieldType.U32: _Cursor.u32,
    _FieldType.I64: _Cursor.i64,
    _FieldType.U64: _Cursor.u64,
    _FieldType.STRINGID: _Cursor.u16,
    _FieldType.STRING: _Cursor.gamma_str,
}


def _read_table_headers(cursor: _Cursor) -> dict:
    """
    Reads a table chunk's full header schema, mirroring openttdlab.py's
    read_table_headers: a flat list of (type, has_length, name) for "root",
    plus one entry per nested STRUCT group, keyed like "root.subfield".
    Needed for every table chunk (not just GSDT) since we must walk past
    each chunk's full header — including nested STRUCT groups — to reach
    its records, even for chunks we otherwise skip byte-for-byte.
    """
    def read_fields():
        while (type_byte := cursor.i8()) != 0:
            yield (_FieldType(type_byte & 0xF), bool(type_byte & 0x10), cursor.gamma_str())

    def read_substruct(header, parent_key):
        for field_type, _has_length, sub_key in header:
            if field_type == _FieldType.STRUCT:
                sub_header = list(read_fields())
                full_sub_key = f"{parent_key}.{sub_key}"
                yield full_sub_key, sub_header
                yield from read_substruct(sub_header, full_sub_key)

    root_header = list(read_fields())
    return {"root": root_header, **dict(read_substruct(root_header, "root"))}


def _read_flat_root_fields(cursor: _Cursor, root_header: list) -> dict:
    """
    Reads one record's root-level fields, for chunks (just GSDT, here)
    confirmed to have a flat schema. Matches openttdlab.py's own special
    case: STRING fields are marked has_length=True (length-prefixed) but
    are still a single string, not a list — only a *true* list (has_length
    on a non-STRING field, or a STRUCT) is unsupported here.
    """
    fields = {}
    for field_type, has_length, name in root_header:
        if field_type == _FieldType.STRUCT or (has_length and field_type != _FieldType.STRING):
            raise NotImplementedError(
                f"GSDT header field '{name}' isn't a flat scalar — schema assumption is stale, "
                f"re-check with --inspect --chunk GSDT."
            )
        fields[name] = _SCALAR_READERS[field_type](cursor)
    return fields


class _SQSL(enum.IntEnum):
    """
    OpenTTD's recursive GameScript Save()-object tags (see
    src/script/script_instance.{cpp,hpp} ScriptInstance::SaveObject /
    SQSaveLoadType). One byte type tag, then type-specific payload;
    arrays/tables are a flat sequence of recursively-encoded elements
    terminated by an END (0xFF) marker.
    """
    INT = 0x00
    STRING = 0x01
    ARRAY = 0x02
    TABLE = 0x03
    BOOL = 0x04
    NULL = 0x05
    INSTANCE = 0x06
    END = 0xFF


_SQSL_END = object()  # sentinel distinct from any decoded value, incl. None


def _decode_sqsl(cursor: _Cursor):
    tag = cursor.u8()
    if tag == _SQSL.INT:
        return cursor.i64()
    if tag == _SQSL.STRING:
        raw = cursor.read(cursor.u8())
        return raw[:-1].decode("utf-8", errors="replace")  # drop trailing NUL
    if tag == _SQSL.ARRAY:
        items = []
        while (item := _decode_sqsl(cursor)) is not _SQSL_END:
            items.append(item)
        return items
    if tag == _SQSL.TABLE:
        table = {}
        while (key := _decode_sqsl(cursor)) is not _SQSL_END:
            table[key] = _decode_sqsl(cursor)
        return table
    if tag == _SQSL.BOOL:
        return bool(cursor.u8())
    if tag == _SQSL.NULL:
        return None
    if tag == _SQSL.END:
        return _SQSL_END
    raise NotImplementedError(f"SQSL tag {tag:#x} (e.g. SQSL_INSTANCE) isn't supported by this decoder.")


def extract_rvg_export_table(path: Path) -> dict:
    """
    Decodes rvg_fork's ::ExportDataTable directly from the GSDT chunk's raw
    bytes. OpenTTDLab only decodes GSDT's fixed name/settings/version
    header and explicitly discards everything after it as "junk"
    (openttdlab.py's read_table_records special-cases GSDT for exactly this
    reason) — that "junk" is the GameScript's own Save()-table, serialized
    with OpenTTD's recursive SQSL object format. See CLAUDE.MD for the full
    reverse-engineering trail.

    Returns {town_id: {name, population, houses, passengers_produced,
    mail_produced}} for the GSDT record whose name matches RVG_SCRIPT_NAME,
    or {} if that record isn't found or hasn't saved any data yet (e.g. the
    modified script hasn't completed its first monthly tick).
    """
    with open(path, "rb") as f:
        raw = f.read()

    compression = raw[:4]
    body = raw[8:]  # 4-byte compression tag + 2-byte savegame_version + 2-byte pad
    if compression == b"OTTN":
        data = body
    elif compression == b"OTTZ":
        data = zlib.decompress(body)
    elif compression == b"OTTX":
        data = lzma.decompress(body)
    else:
        raise ValueError(f"Unsupported savegame compression: {compression!r}")

    cursor = _Cursor(data)
    while (tag_bytes := cursor.read(4)) != b"\0\0\0\0":
        tag = tag_bytes.decode()
        m = cursor.u8()
        chunk_type = m & 0xF

        if chunk_type == 0:  # RIFF chunk (e.g. map tile arrays) — skip whole
            size = (m >> 4) << 24 | cursor.u24()
            cursor.read(size)
            continue

        if chunk_type in (1, 2):  # legacy array chunk — skip record by record
            while size_plus_one := cursor.gamma():
                cursor.read(size_plus_one - 1)
            continue

        if chunk_type in (3, 4):  # table chunk (self-describing)
            cursor.gamma()  # header byte-length; header itself is self-terminating
            root_header = _read_table_headers(cursor)["root"]

            if tag != CHUNK_GAMESCRIPT:
                while size_plus_one := cursor.gamma():
                    cursor.read(size_plus_one - 1)
                continue

            counter = itertools.count()
            while size_plus_one := cursor.gamma():
                record_start = cursor.pos
                cursor.gamma() if chunk_type == 4 else next(counter)  # index, unused
                fields = _read_flat_root_fields(cursor, root_header)

                remaining = size_plus_one - 1 - (cursor.pos - record_start)
                blob = cursor.read(remaining)

                if fields.get("name") == RVG_SCRIPT_NAME and blob and blob[0] == 1:
                    save_table = _decode_sqsl(_Cursor(blob, pos=1))  # skip presence byte
                    return save_table.get("export_data_table", {})
            continue

        raise ValueError(f"Unknown chunk type {chunk_type} for tag {tag}.")

    return {}


def parse_sav_file(path: Path) -> dict:
    """Parse a single .sav file into OpenTTDLab's nested dict structure."""
    with open(path, "rb") as f:
        return parse_savegame(iter(lambda: f.read(65536), b""))


def inspect_savegame(path: Path, sample_count: int = 1, only_chunk: str = None) -> None:
    """
    Print the top-level chunk keys and up to `sample_count` sample RECORDs
    (not header/schema) from every chunk present in the savegame (or just
    `only_chunk`, if given). This confirms the parser is working and shows
    real field values to sanity-check against the extraction functions
    below, and lets us spot chunks we haven't considered yet (e.g. a
    population-bearing chunk for towns).
    """
    parsed = parse_sav_file(path)
    chunks = parsed.get("chunks", {})

    print("Top-level chunks found in savegame:")
    for key in sorted(chunks.keys()):
        print(f"  {key}")

    chunk_ids = [only_chunk] if only_chunk else sorted(chunks.keys())
    for chunk_id in chunk_ids:
        print(f"\n--- {chunk_id} ---")
        chunk_data = chunks.get(chunk_id)
        if not chunk_data:
            print(f"  Chunk {chunk_id} not found or empty.")
            continue

        headers = chunk_data.get("headers", {})
        records = chunk_data.get("records", {})

        print(f"  Header schema keys: {sorted(headers.keys())}")
        print(f"  Number of records: {len(records)}")

        record_keys = list(itertools.islice(records.keys(), sample_count))
        if record_keys:
            for record_key in record_keys:
                print(f"  Sample record (index {record_key}):")
                print(json.dumps(records[record_key], indent=2, default=str))
        else:
            print("  (no records — chunk is empty on this map)")


def _first(struct_list):
    """
    Fields defined as STRUCT with has_length=True decode as a *list* of
    sub-records (often length 0 or 1 in practice, e.g. a station's
    'normal' vs 'waypoint' branch, or a vehicle's 'train' vs 'roadveh'
    branch — only one is ever actually populated). This helper safely
    grabs the first item, or returns None if the list is empty.
    """
    if isinstance(struct_list, list) and struct_list:
        return struct_list[0]
    return None


def extract_towns(parsed: dict, rvg_export: dict = None) -> list[dict]:
    """
    Extract per-town data from a parsed savegame. The CITY chunk itself has
    no population field and 'name' is blank for any procedurally-named town
    (confirmed exhaustively — see CLAUDE.MD), so population/houses/cargo
    production come from rvg_export (see extract_rvg_export_table), keyed
    by town_id, and take priority over CITY's own (usually blank) name.
    """
    rvg_export = rvg_export or {}
    chunk = parsed.get("chunks", {}).get(CHUNK_TOWNS, {})
    records = chunk.get("records", {})
    rows = []
    for town_id, town in records.items():
        rvg_data = rvg_export.get(int(town_id), {})
        rows.append({
            "town_id": town_id,
            "name": rvg_data.get("name") or town.get("name", ""),
            "population": rvg_data.get("population", ""),
            "houses": rvg_data.get("houses", ""),
            "passengers_produced": rvg_data.get("passengers_produced", ""),
            "mail_produced": rvg_data.get("mail_produced", ""),
        })
    return rows


def extract_stations(parsed: dict) -> list[dict]:
    """
    Extract station name, owning town, and cargo/rating info.
    Confirmed structure from --inspect (STNN headers): a station record's
    'normal' field is a list (usually 0 or 1 items — empty if this
    record is actually a waypoint instead). Within that, 'base' is
    itself a list containing the actual name/town/owner fields, and
    'goods' is a list of per-cargo-type rating/waiting records.
    """
    chunk = parsed.get("chunks", {}).get(CHUNK_STATIONS, {})
    records = chunk.get("records", {})
    rows = []
    for station_id, station in records.items():
        normal = _first(station.get("normal", []))
        if normal is None:
            continue  # this record is a waypoint, not a station — skip
        base = _first(normal.get("base", []))
        if base is None:
            continue

        goods_list = normal.get("goods", [])
        # goods_list is a list of per-cargo-type dicts with 'rating', etc.
        # Cargo TYPE isn't itself in this structure — it's implied by
        # position/index, cross-referenced against the game's cargo
        # type table (a separate lookup, not handled here yet).
        rows.append({
            "station_id": station_id,
            "name": base.get("name", ""),
            "town_id": base.get("town", ""),
            "owner": base.get("owner", ""),
            "facilities": base.get("facilities", ""),
            "num_cargo_types_with_goods_data": len(goods_list),
            "goods_raw": json.dumps(goods_list, default=str),
        })
    return rows


def extract_vehicles(parsed: dict) -> list[dict]:
    """
    Extract vehicle identity, type, and status info.
    Confirmed structure from --inspect (VEHS headers): only ONE of
    'train' / 'roadveh' / 'ship' / 'aircraft' will actually be populated
    per record (a list with one item), matching the vehicle's 'type'
    field. Each populated one has a nested 'common' struct (also a
    single-item list) holding name/owner/cargo/order fields.
    """
    chunk = parsed.get("chunks", {}).get(CHUNK_VEHICLES, {})
    records = chunk.get("records", {})
    rows = []
    for vehicle_id, vehicle in records.items():
        vtype = None
        common = None
        for kind in ("train", "roadveh", "ship", "aircraft"):
            sub = _first(vehicle.get(kind, []))
            if sub is not None:
                vtype = kind
                common = _first(sub.get("common", []))
                break
        if common is None:
            continue  # effect/disaster vehicle, or genuinely empty slot

        rows.append({
            "vehicle_id": vehicle_id,
            "type": vtype,
            "name": common.get("name", ""),
            "owner": common.get("owner", ""),
            "cargo_type": common.get("cargo_type", ""),
            "cargo_cap": common.get("cargo_cap", ""),
            "profit_this_year": common.get("profit_this_year", ""),
            "age": common.get("age", ""),
        })
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    if not rows:
        print(f"  (no rows to write for {path.name})")
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def process_one_save(sav_path: Path, out_dir: Path) -> None:
    print(f"Processing {sav_path.name}...")
    parsed = parse_sav_file(sav_path)

    try:
        rvg_export = extract_rvg_export_table(sav_path)
    except Exception as e:
        print(f"  Warning: couldn't decode RVG export data ({e}); towns will be missing population/houses/cargo.")
        rvg_export = {}

    stamp = sav_path.stem  # use the autosave's own filename as the label
    write_csv(extract_towns(parsed, rvg_export), out_dir / f"{stamp}_towns.csv")
    write_csv(extract_stations(parsed), out_dir / f"{stamp}_stations.csv")
    write_csv(extract_vehicles(parsed), out_dir / f"{stamp}_vehicles.csv")


def watch_and_process(watch_dir: Path, out_dir: Path, poll_seconds: int = 30) -> None:
    """
    Simple polling watcher (no extra dependency beyond OpenTTDLab/pandas).
    Tracks already-processed files by name so re-running doesn't redo work.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    processed_marker = out_dir / ".processed.json"
    processed = set()
    if processed_marker.exists():
        processed = set(json.loads(processed_marker.read_text()))

    print(f"Watching {watch_dir} every {poll_seconds}s. Ctrl+C to stop.")
    try:
        while True:
            sav_files = sorted(watch_dir.glob("*.sav"))
            new_files = [f for f in sav_files if f.name not in processed]
            for f in new_files:
                try:
                    process_one_save(f, out_dir)
                    processed.add(f.name)
                    processed_marker.write_text(json.dumps(sorted(processed)))
                except Exception as e:
                    print(f"  Failed to process {f.name}: {e}")
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        print("Stopped.")


def main():
    parser = argparse.ArgumentParser(description="OpenTTD monthly data extractor")
    parser.add_argument("--watch-dir", type=str, help="Directory to watch for autosave .sav files")
    parser.add_argument("--out-dir", type=str, default="./extracted_data", help="Where to write extracted CSVs")
    parser.add_argument("--inspect", type=str, help="Path to a single .sav file to inspect chunk structure")
    parser.add_argument("--sample-count", type=int, default=1, help="Number of sample records to print per chunk with --inspect")
    parser.add_argument("--chunk", type=str, help="Restrict --inspect to a single chunk ID (e.g. CITY)")
    parser.add_argument("--dump-rvg-export", type=str, help="Path to a .sav file; decode and print rvg_fork's GSDT export table for debugging")
    parser.add_argument("--poll-seconds", type=int, default=30, help="Polling interval in seconds")
    args = parser.parse_args()

    if args.inspect:
        inspect_savegame(Path(args.inspect), args.sample_count, args.chunk)
        return

    if args.dump_rvg_export:
        export = extract_rvg_export_table(Path(args.dump_rvg_export))
        print(json.dumps(export, indent=2, default=str))
        return

    if not args.watch_dir:
        parser.error("--watch-dir is required unless using --inspect")

    watch_and_process(Path(args.watch_dir), Path(args.out_dir), args.poll_seconds)


if __name__ == "__main__":
    main()