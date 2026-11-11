#!/usr/bin/env python3
import glob
import json
import sqlite3

CANDIDATES = [
    "/usr/local/apps/@appdata/trim.media/database",
    "/vol2/@appdata/trim.media/database",
    "/vol1/@appdata/trim.media/database",
]
SAMPLE_TABLES = ("item", "genre", "person", "episode", "poster", "media_info",
                 "video_stream", "audio_stream", "subtitle")


def ro_connect(path: str) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro&immutable=0"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def probe(path: str) -> dict:
    out = {"path": path, "tables": {}}
    with ro_connect(path) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for name in names:
            cols = [{"name": r[1], "type": r[2]} for r in conn.execute(
                f"PRAGMA table_info('{name}')")]
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM '{name}'").fetchone()[0]
            except sqlite3.Error:
                count = -1
            table = {"columns": cols, "rows": count, "sample": None}
            if count and any(k in name.lower() for k in SAMPLE_TABLES):
                try:
                    row = conn.execute(
                        f"SELECT * FROM '{name}' LIMIT 1").fetchone()
                    if row:
                        table["sample"] = {k: str(v)[:80]
                                           for k, v in dict(row).items()}
                except sqlite3.Error:
                    pass
            if "genre" in name.lower() and count and count < 200:
                try:
                    table["rows_all"] = [dict(r) for r in conn.execute(
                        f"SELECT * FROM '{name}'")]
                except sqlite3.Error:
                    pass
            out["tables"][name] = table
    return out


def main() -> None:
    result = {"databases": [], "not_found": []}
    for d in CANDIDATES:
        for db in glob.glob(d + "/*.db"):
            try:
                result["databases"].append(probe(db))
            except (sqlite3.Error, OSError) as exc:
                result["databases"].append({"path": db, "error": str(exc)})
    if not result["databases"]:
        result["not_found"] = CANDIDATES
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
