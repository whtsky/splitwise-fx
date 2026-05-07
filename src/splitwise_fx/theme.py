"""Rich theme + console factory.

Define styles once with semantic names; reference them everywhere by name. No
hard-coded colors scattered across the code base.
"""

from __future__ import annotations

from rich.console import Console
from rich.style import Style
from rich.theme import Theme

THEME = Theme(
    {
        # Severities
        "error": Style(color="red", bold=True),
        "warn": Style(color="yellow"),
        "ok": Style(color="green", bold=True),
        "info": Style(color="cyan"),
        "muted": Style(dim=True),
        # Headings / emphasis
        "heading": Style(bold=True),
        "accent": Style(color="cyan", bold=True),
        # Currency amounts
        "amount.src": Style(color="yellow"),  # original currency
        "amount.dst": Style(color="green", bold=True),  # target currency
        # Rate-source tags
        "tag.upi": Style(color="cyan", bold=True),
        "tag.fra": Style(color="magenta", bold=True),
        # Cache-stat counters
        "stat.memory": Style(color="cyan", bold=True),
        "stat.disk": Style(color="magenta", bold=True),
        "stat.network": Style(color="yellow", bold=True),
        # Status glyphs
        "glyph.ok": Style(color="green", bold=True),
        "glyph.fail": Style(color="red", bold=True),
        "glyph.skip": Style(color="yellow"),
    }
)


def make_console(*, stderr: bool = False) -> Console:
    return Console(theme=THEME, stderr=stderr, highlight=False)
