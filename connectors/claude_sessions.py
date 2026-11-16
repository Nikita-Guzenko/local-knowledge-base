"""Index Claude Code JSONL sessions from explicit KB_CLAUDE_ROOT.

Only user and assistant messages are indexed; injected metadata and large tool
results are omitted. Sessions modified within 120 seconds are skipped until
settled. Missing source configuration fails instead of scanning the home folder.
"""
import json
import os
import re
import time

import kbdb
from connectors._config import required_path, walk_error

SOURCE = "claude"
ACTIVE_SECONDS = 120
MSG_CAP = 20000      # chars kept per user/assistant message
TOOL_LINE = 200      # chars per tool-call line
RESULT_SHORT = 300   # tool results up to this length are kept whole
ERROR_HEAD = 500     # error results: first N chars

_REMINDER = re.compile(r"<system-reminder>[\s\S]*?(?:</system-reminder>|$)")
_B64 = re.compile(r"(?:data:[\w/+.-]+;base64,)?[A-Za-z0-9+/]{300,}={0,2}")
_ERRORISH = re.compile(r"(?i)\b(error|traceback|exception|failed|fatal|exit code [1-9]|"
                       r"process exited with code [1-9]|command not found|permission denied)\b")


def clean(text, cap=MSG_CAP):
    """Strip reminders and base64, cap length. Shared with codex_sessions."""
    if not text:
        return ""
    text = _REMINDER.sub("", text)
    text = _B64.sub("[base64]", text)
    text = text.strip()
    if len(text) > cap:
        text = text[:cap] + f" …[cut {len(text) - cap} chars]"
    return text


def one_line(text, cap):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= cap else text[:cap - 1] + "…"


def result_line(text, is_error=False):
    """Tool result -> one line, or None when it is long and not an error."""
    text = _B64.sub("[base64]", _REMINDER.sub("", text or "")).strip()
    if not text:
        return None
    if is_error or _ERRORISH.search(text[:ERROR_HEAD]):
        if is_error or len(text) > RESULT_SHORT:
            return "ERROR: " + one_line(text, ERROR_HEAD)
    if len(text) <= RESULT_SHORT:
        return "RESULT: " + one_line(text, RESULT_SHORT)
    return None


_KEY_ARGS = ("command", "cmd", "file_path", "path", "pattern", "query", "url", "skill",
             "description", "prompt", "subagent_type", "action", "to", "message")


def tool_line(name, args):
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return "TOOL: " + one_line(f"{name}: {args}", TOOL_LINE)
    if isinstance(args, dict):
        parts = [f"{k}={args[k]}" for k in _KEY_ARGS if isinstance(args.get(k), (str, int)) and args.get(k)]
        arg = " ".join(parts[:2]) if parts else json.dumps(args, ensure_ascii=False)
    else:
        arg = str(args)
    return "TOOL: " + one_line(f"{name}: {arg}", TOOL_LINE)


def _blocks_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def iter_files(root):
    for dirpath, dirs, files in os.walk(root, onerror=walk_error):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv"} and not os.path.islink(os.path.join(dirpath, d))]
        for f in files:
            if f.endswith(".jsonl") and not os.path.islink(os.path.join(dirpath, f)):
                yield os.path.join(dirpath, f)


def needs_ingest(con, source, path, st):
    if time.time() - st.st_mtime < ACTIVE_SECONDS:
        return None
    wm = f"{st.st_mtime_ns}:{st.st_size}"
    return None if kbdb.get_wm(con, source, path) == wm else wm


def parse(path):
    """Stream one file -> (ts, cwd, first_prompt, transcript) ; first_prompt None = empty session."""
    ts = cwd = first = None
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            t = d.get("type")
            if t not in ("user", "assistant") or d.get("isMeta"):
                continue
            ts = ts or d.get("timestamp")
            cwd = cwd or d.get("cwd")
            msg = d.get("message") or {}
            content = msg.get("content")
            if t == "user":
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                for b in content or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        txt = clean(b.get("text"))
                        if txt:
                            out.append("USER: " + txt)
                            if first is None and not txt.startswith("<"):
                                first = txt
                    elif b.get("type") == "tool_result":
                        r = result_line(_blocks_text(b.get("content")), bool(b.get("is_error")))
                        if r:
                            out.append(r)
            else:
                for b in content or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        txt = clean(b.get("text"))
                        if txt:
                            out.append("ASSISTANT: " + txt)
                    elif b.get("type") == "tool_use":
                        out.append(tool_line(b.get("name"), b.get("input")))
    if first is None and any(x.startswith("USER: ") for x in out):
        first = next(x[6:] for x in out if x.startswith("USER: "))
    return ts, cwd, first, "\n".join(out)


def run(con, root=None, source=SOURCE, parser=parse, ident=None):
    root = root or required_path("KB_CLAUDE_ROOT")
    n = 0
    for path in iter_files(root):
        try:
            st = os.stat(path)
        except OSError:
            continue
        wm = needs_ingest(con, source, path, st)
        if wm is None:
            continue
        rel = os.path.relpath(path, root)
        source_id = ident(rel) if ident else rel[:-len(".jsonl")]
        ts, cwd, first, text = parser(path)
        if first is None:   # no user text: empty session; remember it so it is not re-read
            kbdb.replace_rows(con, source, source_id, [])
            kbdb.set_wm(con, source, path, wm)
            continue
        proj = os.path.basename((cwd or "").rstrip("/")) or rel.split(os.sep)[0]
        title = f"{proj}: {one_line(first, 100)}"
        ts = ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))
        meta = {"path": path, "cwd": cwd, "mtime": int(st.st_mtime), "size": st.st_size}
        kbdb.replace_rows(con, source, source_id, kbdb.item_rows(ts, title, text, meta))
        kbdb.set_wm(con, source, path, wm)
        n += 1
    return n
