"""Loads trusted package templates and inlines separated CSS/JavaScript assets."""

from pathlib import Path
from typing import Mapping, Optional


WEB_ROOT = Path(__file__).resolve().parent


def render_document(name: str, replacements: Optional[Mapping[str, str]] = None) -> bytes:
    """Renders one known web shell without adding caller-controlled file access."""

    if name not in {"cycles", "login", "report", "probe", "users"}:
        raise ValueError("unknown web document")
    template = (WEB_ROOT / "templates" / (name + ".html")).read_text(encoding="utf-8")
    stylesheet = (WEB_ROOT / "static" / (name + ".css")).read_text(encoding="utf-8")
    script = (WEB_ROOT / "static" / (name + ".js")).read_text(encoding="utf-8")
    document = template.replace("__INLINE_CSS__", stylesheet).replace(
        "__INLINE_SCRIPT__", script
    )
    for marker, value in (replacements or {}).items():
        document = document.replace(marker, value)
    return document.encode("utf-8")
