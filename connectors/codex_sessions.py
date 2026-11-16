"""Index Codex JSONL sessions from explicit KB_CODEX_ROOT.

Only response_item records are indexed. Event echoes, injected instructions,
encrypted reasoning, and copied parent history are omitted.
"""
import json
import os
import re

from connectors import claude_sessions as cs
from connectors._config import required_path

SOURCE = "codex"
_INJECTED = ("# AGENTS.md instructions", "<environment_context>", "<INSTRUCTIONS>")
_KEEP = {"session_meta", "response_item"}
_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")


def _out_text(output):
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(b.get("text", "") for b in output if isinstance(b, dict) and b.get("type") == "input_text")
    return ""


def parse(path):
    ts = cwd = first = None
    start_ord = 0
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("type") not in _KEEP:
                continue
            p = d.get("payload") or {}
            if not isinstance(p, dict):
                continue
            if d.get("type") == "session_meta":
                if ts is None:
                    ts = p.get("timestamp") or d.get("timestamp")
                    cwd = p.get("cwd")
                    start_ord = p.get("subagent_history_start_ordinal") or 0
                continue
            if (d.get("ordinal") or 0) < start_ord:
                continue
            ts = ts or d.get("timestamp")
            pt = p.get("type")
            if pt == "message":
                role = p.get("role")
                if role not in ("user", "assistant"):
                    continue
                for b in p.get("content") or []:
                    if not isinstance(b, dict) or b.get("type") not in ("input_text", "output_text"):
                        continue
                    raw = b.get("text") or ""
                    if role == "user" and (raw.lstrip().startswith(_INJECTED) or raw.strip() in ("<image>", "</image>")):
                        continue
                    txt = cs.clean(raw)
                    if not txt:
                        continue
                    out.append(("USER: " if role == "user" else "ASSISTANT: ") + txt)
                    if role == "user" and first is None:
                        first = txt
            elif pt in ("custom_tool_call", "function_call"):
                name = p.get("name") or "?"
                if p.get("namespace"):
                    name = f"{p['namespace']}.{name}"
                out.append(cs.tool_line(name, p.get("input") if pt == "custom_tool_call" else p.get("arguments")))
            elif pt in ("custom_tool_call_output", "function_call_output"):
                r = cs.result_line(_out_text(p.get("output")))
                if r:
                    out.append(r)
    return ts, cwd, first, "\n".join(out)


def _ident(rel):
    m = _ID.search(rel)
    return m.group(1) if m else rel[:-len(".jsonl")]


def run(con):
    return cs.run(con, root=required_path("KB_CODEX_ROOT"), source=SOURCE, parser=parse, ident=_ident)
