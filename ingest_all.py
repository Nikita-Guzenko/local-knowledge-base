#!/usr/bin/env python3
"""Ingest explicitly selected sources into a local SQLite knowledge base.

  KB_FILES_ROOTS=/path/to/notes python3 ingest_all.py --only files
  KB_CLAUDE_ROOT=/path/to/sessions python3 ingest_all.py --only claude_sessions
  KB_CODEX_ROOT=/path/to/sessions python3 ingest_all.py --only codex_sessions
  KB_JSONL_PATH=/path/to/export.jsonl python3 ingest_all.py --only jsonl

Comma-separated selections are supported. No source is enabled by default.
Failures roll back that connector and return a nonzero exit status; other
selected connectors still run. No model or network calls are automatic.
"""
import argparse
import importlib
from pathlib import Path
import sys
import time

import kbdb

CONNECTORS = ("files", "claude_sessions", "codex_sessions", "jsonl")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", required=True, help="comma-separated connectors: " + ", ".join(CONNECTORS))
    parser.add_argument("--no-summarize", action="store_true", help="compatibility flag; summaries are always a separate explicit command")
    args = parser.parse_args()
    names = [name.strip() for name in args.only.split(",")]
    if any(name not in CONNECTORS for name in names):
        parser.error("unknown connector; choose from " + ", ".join(CONNECTORS))
    names = list(dict.fromkeys(names))
    log_path = Path(kbdb.DB_PATH).expanduser().resolve().parent / "ingest.log"
    con = kbdb.connect()
    failed = 0
    try:
        for name in names:
            start = time.monotonic()
            source = name
            try:
                module = importlib.import_module(f"connectors.{name}")
                source = module.SOURCE
                count = module.run(con)
                note = f"{time.monotonic() - start:.2f}s"
                con.execute("INSERT INTO ingest_log(ts,source,ok,items,note) VALUES (?,?,1,?,?)",
                            (kbdb.now_iso(), source, count, note))
                con.commit()
                message = f"ok {name}: {count} items in {note}"
            except Exception as exc:
                con.rollback()
                failed += 1
                # Exceptions may contain source data; record only the failure class in persistent logs.
                note = type(exc).__name__
                con.execute("INSERT INTO ingest_log(ts,source,ok,items,note) VALUES (?,?,0,0,?)",
                            (kbdb.now_iso(), source, note))
                con.commit()
                message = f"FAIL {name}: {type(exc).__name__}: {exc}"
            print(message, flush=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{kbdb.now_iso()} {name} {'failed' if message.startswith('FAIL') else 'ok'}\n")
    finally:
        con.close()
    return int(bool(failed))


if __name__ == "__main__":
    sys.exit(main())
