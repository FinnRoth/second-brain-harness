"""Open-WebUI-spezifische Eigenheiten."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from .memory.retrieval import text_of

# Open WebUI schickt Hintergrund-Aufgaben (Titel, Tags, Follow-ups, Suchanfragen …)
# an dasselbe Modell. Diese erkennen wir am Prompt-Aufbau.
_TASK_MARKERS = (
    "### Task:",
    "### Guidelines:",
    "### Output:",
    "<chat_history>",
)

ACTIVITY_RE = re.compile(r'<details type="memory"[^>]*>.*?</details>\s*', re.DOTALL)


def is_task_request(body: Mapping[str, Any]) -> bool:
    meta = body.get("metadata")
    if isinstance(meta, dict) and meta.get("task"):
        return True
    messages = body.get("messages") or []
    if not messages:
        return False
    last = text_of(messages[-1].get("content"))
    head = last[:600]
    return "### Task:" in head or sum(m in last for m in _TASK_MARKERS) >= 2


def conversation_id(headers: Mapping[str, str], messages: list[dict[str, Any]]) -> str:
    chat_id = headers.get("x-openwebui-chat-id") or headers.get("x-chat-id")
    if chat_id:
        return chat_id
    # Fallback: stabiler Hash über die erste Nutzernachricht
    first = next((text_of(m.get("content")) for m in messages if m.get("role") == "user"), "")
    return "h-" + hashlib.sha256(first.encode("utf-8")).hexdigest()[:20]


def strip_activity(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Entfernt unsere eigenen Aktivitätsblöcke aus früheren Assistenten-Nachrichten."""
    out = []
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("content"), str) and "<details" in m["content"]:
            m = {**m, "content": ACTIVITY_RE.sub("", m["content"])}
        out.append(m)
    return out


def activity_block(lines: list[str]) -> str:
    """Einklappbare Aktivitätsanzeige (Tool-Aufrufe) für Open WebUI."""
    if len(lines) == 1:
        title = lines[0] if not lines[0][:1].isalnum() else f"🧠 {lines[0]}"
        return f'<details type="memory">\n<summary>{title}</summary>\n</details>\n\n'
    items = "\n".join(f"- {line}" for line in lines)
    return f'<details type="memory">\n<summary>⚙️ {len(lines)} Aktionen</summary>\n\n{items}\n</details>\n\n'
