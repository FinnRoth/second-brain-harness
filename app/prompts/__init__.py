"""Prompt-Vorlagen. Platzhalter im Format {{name}} werden per render() ersetzt.

Die Dateien können frei angepasst werden; optional lassen sie sich über
PROMPTS_DIR (Umgebungsvariable) durch eigene Versionen überschreiben.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

_BASE = Path(__file__).parent


@lru_cache
def load(name: str) -> str:
    override = os.environ.get("PROMPTS_DIR")
    if override and (Path(override) / f"{name}.md").exists():
        return (Path(override) / f"{name}.md").read_text(encoding="utf-8")
    return (_BASE / f"{name}.md").read_text(encoding="utf-8")


def render(name: str, **values: object) -> str:
    text = load(name)
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text
