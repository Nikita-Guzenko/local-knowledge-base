"""Import normalized JSONL exports from explicit KB_JSONL_PATH.

Each record requires string id, timestamp (ISO 8601), title, content, and a
nonempty path or url. Optional source defaults to 'import'; metadata is an object.
Stable (source,id) identifies an item. Unchanged records retain their summaries.
The whole file is validated before writes. Missing records are not deletions.
"""
import hashlib
import json
from datetime import datetime, timezone

import kbdb
from connectors._config import required_path

SOURCE = "import"


def _records(path):
    records = []
    seen = set()
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("record must be an object")
                for key in ("id", "timestamp", "title", "content"):
                    if not isinstance(item.get(key), str) or not item[key].strip():
                        raise ValueError(f"{key} must be a nonempty string")
                stamp = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                item["timestamp"] = stamp.astimezone(timezone.utc).isoformat(timespec="seconds")
                source = item.get("source", SOURCE)
                if not isinstance(source, str) or not source.strip():
                    raise ValueError("source must be a nonempty string")
                metadata = item.get("metadata", {})
                if not isinstance(metadata, dict):
                    raise ValueError("metadata must be an object")
                metadata = dict(metadata)
                for key in ("path", "url"):
                    if key in item:
                        if not isinstance(item[key], str) or not item[key].strip():
                            raise ValueError(f"{key} must be a nonempty string")
                        metadata[key] = item[key]
                if not any(isinstance(metadata.get(key), str) and metadata[key].strip() for key in ("path", "url")):
                    raise ValueError("record requires a path or url")
                identity = (source, item["id"])
                if identity in seen:
                    raise ValueError("duplicate (source,id) in export")
                seen.add(identity)
                content = [item["timestamp"], item["title"], item["content"], metadata]
                mark = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                records.append((source, item, metadata, mark))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Invalid JSONL record at line {number}: {exc}") from exc
    return records


def run(con):
    records = _records(required_path("KB_JSONL_PATH", directory=False))
    changed = 0
    for source, item, metadata, mark in records:
        key = "jsonl:" + item["id"]
        if kbdb.get_wm(con, source, key) == mark:
            continue
        kbdb.replace_rows(con, source, item["id"], kbdb.item_rows(
            item["timestamp"], item["title"], item["content"], metadata))
        kbdb.set_wm(con, source, key, mark)
        changed += 1
    return changed
