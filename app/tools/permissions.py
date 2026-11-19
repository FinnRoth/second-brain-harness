"""Freigabe-Modi – angelehnt an Claude Code.

  plan  – nur lesen: schreibende Tools werden gar nicht angeboten
  ask   – lesende Tools laufen sofort, schreibende brauchen deine Freigabe (Standard)
  auto  – alles läuft ohne Rückfrage (außer Tools mit expliziter ask-/deny-Regel)

Gedächtnis-Tools (remember, forget …) sind in allen Modi erlaubt, im Inkognito-Chat ausgeblendet.
Explizite Regeln aus der MCP-Konfiguration gehen immer vor: deny blendet aus, ask fragt immer,
allow erlaubt auch schreibende Tools ohne Rückfrage (außer im Plan-Modus).
"""

from __future__ import annotations

import re
from typing import Literal

from .base import ToolContext, ToolSpec

PERMISSION_MODES = ("plan", "ask", "auto")
MODE_ALIASES = {"plan": "plan", "planen": "plan", "ask": "ask", "fragen": "ask", "auto": "auto"}
MODE_DESCRIPTIONS = {
    "plan": "Planungsmodus: Du darfst nur lesende Tools nutzen. Erarbeite einen Plan und führe nichts aus, "
    "was etwas verändert. Wenn der Plan steht, kann der Nutzer mit /ask oder /auto in einen ausführenden Modus wechseln.",
    "ask": "Freigabe-Modus „ask“: Lesende Tools laufen sofort. Aktionen, die etwas verändern, brauchen die "
    "Freigabe des Nutzers – der Harness fragt automatisch nach.",
    "auto": "Freigabe-Modus „auto“: Alle Tools laufen ohne Rückfrage. Sei bei verändernden Aktionen trotzdem umsichtig.",
}

Decision = Literal["allow", "ask", "hidden"]


def decide(spec: ToolSpec, ctx: ToolContext) -> Decision:
    if spec.policy == "deny":
        return "hidden"
    if spec.kind == "memory":
        return "hidden" if ctx.incognito else "allow"
    if spec.kind == "read":
        return "ask" if spec.policy == "ask" and spec.name not in ctx.allowed_tools else "allow"
    # schreibend
    if ctx.permission_mode == "plan":
        return "hidden"
    if spec.name in ctx.allowed_tools:
        return "allow"
    if spec.policy == "ask":
        return "ask"
    if spec.policy == "allow" or ctx.permission_mode == "auto":
        return "allow"
    return "ask"


_COMMAND_RE = re.compile(r"^\s*/(plan|planen|ask|fragen|auto)\b[ \t]*\n?", re.IGNORECASE)


def parse_mode_command(text: str) -> tuple[str | None, str]:
    """'/plan Bau mir X' → ('plan', 'Bau mir X')."""
    m = _COMMAND_RE.match(text or "")
    if not m:
        return None, text
    return MODE_ALIASES[m.group(1).lower()], text[m.end() :]


# ------------------------------------------------------- Freigabe per Chat-Antwort
_YES_CORE = {
    "ja", "jap", "jo", "jup", "yes", "y", "ok", "okay", "okey", "klar", "mach", "go", "los", "passt",
    "bestätigt", "bestätige", "freigeben", "freigegeben", "einverstanden", "sure", "ausführen", "👍", "yep",
}
_YES_FILLER = {"das", "es", "mal", "bitte", "gerne", "gern", "do", "it", "nur", "zu", "führ", "aus", "doch", "alles", "beides"}
_NO_CORE = {"nein", "ne", "nö", "no", "nope", "stopp", "stop", "abbrechen", "abbruch", "lass", "lieber", "👎"}
_NO_FILLER = {"das", "es", "bitte", "doch", "nicht", "nicht", "sein", "mal", "danke", "lieber", "nicht."}


def classify_reply(text: str) -> Literal["yes", "no", "other"]:
    """Erkennt kurze Zustimmung/Ablehnung auf eine Freigabe-Frage."""
    t = (text or "").strip().lower()
    if not t or len(t) > 60:
        return "other"
    words = [w for w in re.split(r"[\s,.!?;:]+", t) if w]
    if not words:
        return "other"
    if any(w in _YES_CORE for w in words) and all(w in _YES_CORE or w in _YES_FILLER for w in words):
        return "yes"
    if any(w in _NO_CORE for w in words) and all(w in _NO_CORE or w in _NO_FILLER for w in words):
        return "no"
    return "other"
