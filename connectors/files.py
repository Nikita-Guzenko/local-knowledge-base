"""Index .md and .txt notes from explicit KB_FILES_ROOTS (os.pathsep-separated).

Files larger than 2 MiB, backup files, and symbolic links are omitted. A changed
file replaces its rows; deleted/excluded files are removed only under roots
successfully scanned in this run. Missing roots fail before database writes.
"""
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import kbdb
from connectors._config import walk_error

SOURCE = "files"
MAX_BYTES = 2 * 1024 * 1024
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv"}
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.M)


def run(con):
    raw = os.environ.get("KB_FILES_ROOTS", "")
    if not raw.strip():
        raise ValueError("Set KB_FILES_ROOTS to explicit note directories before selecting files")
    roots = []
    for part in raw.split(os.pathsep):
        if not part.strip():
            raise ValueError("KB_FILES_ROOTS contains an empty directory")
        root = Path(part).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("KB_FILES_ROOTS entries must be readable directories")
        roots.append(str(root))
    found = {}
    # Complete discovery before changing rows, so inaccessible subtrees cannot look deleted.
    for root in roots:
        for directory, dirs, names in os.walk(root, onerror=walk_error):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(directory, d))]
            for name in names:
                path = os.path.join(directory, name)
                if name.lower().endswith((".md", ".txt")) and ".bak" not in name and not os.path.islink(path):
                    st = os.stat(path)
                    if st.st_size <= MAX_BYTES:
                        found.setdefault(path, (root, st))
    changed = 0
    for path, (root, st) in sorted(found.items()):
        mark = f"{st.st_mtime_ns}:{st.st_size}"
        if kbdb.get_wm(con, SOURCE, path) == mark:
            continue
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        rel = os.path.join(os.path.basename(root), os.path.relpath(path, root))
        heading = _HEADING.search(text[:20000])
        title = f"{rel} — {heading.group(1)[:200]}" if heading else rel
        ts = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds")
        meta = {"path": path, "root": root, "size": st.st_size}
        kbdb.replace_rows(con, SOURCE, path, kbdb.item_rows(ts, title, text, meta))
        kbdb.set_wm(con, SOURCE, path, mark)
        changed += 1
    for (path,) in con.execute("SELECT key FROM ingest_state WHERE source=?", (SOURCE,)).fetchall():
        if any(path.startswith(root + os.sep) for root in roots) and path not in found:
            kbdb.replace_rows(con, SOURCE, path, [])
            con.execute("DELETE FROM ingest_state WHERE source=? AND key=?", (SOURCE, path))
            changed += 1
    return changed
