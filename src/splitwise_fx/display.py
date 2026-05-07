"""Rich-based preview rendering and progress helpers.

All styling goes through theme names defined in `theme.py`. Code here uses
`Text(..., style="<name>")` or `[<name>]…[/]` markup — never raw colors.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from decimal import Decimal

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.status import Status
from rich.table import Table
from rich.text import Text

from .models import CacheStats, ConvertedExpense, CurrencyCode

_SRC_TAG: Mapping[str, tuple[str, str]] = {
    "unionpay": ("UPI", "tag.upi"),
    "frankfurter": ("FRA", "tag.fra"),
}


def render_preview(
    console: Console,
    converted: list[ConvertedExpense],
    *,
    target: CurrencyCode,
) -> None:
    """Print a Rich table summarizing each converted expense + totals panel."""

    if not converted:
        console.print(
            Panel(
                Text.from_markup(
                    f"No expenses to convert (target currency: [heading]{target}[/])."
                ),
                style="warn",
                box=box.ROUNDED,
            )
        )
        return

    table = Table(
        title=Text.from_markup(f"Currency conversion preview → [heading]{target}[/]"),
        title_justify="left",
        box=box.SIMPLE_HEAD,
        header_style="heading",
        show_lines=False,
        expand=False,
    )
    table.add_column("Date", style="muted", no_wrap=True)
    table.add_column("Description", overflow="fold", max_width=42)
    table.add_column("Original", justify="right", style="amount.src", no_wrap=True)
    table.add_column("", justify="center", style="muted")
    table.add_column(f"Target ({target})", justify="right", style="amount.dst", no_wrap=True)
    table.add_column("Rate", justify="right", style="muted", no_wrap=True)
    table.add_column("Src", no_wrap=True, justify="center")

    total = Decimal(0)
    by_source: dict[CurrencyCode, Decimal] = defaultdict(lambda: Decimal(0))
    for c in converted:
        e = c.expense
        by_source[e.currency_code] += Decimal(e.cost)
        total += c.new_cost

        original = f"{_fmt_amount(Decimal(e.cost))} {e.currency_code}"
        rate = f"{c.rate.normalize():f}"
        label, style_name = _SRC_TAG.get(c.rate_source, (c.rate_source[:3].upper(), "info"))
        src_tag = Text(label, style=style_name)

        table.add_row(
            e.date.date().isoformat(),
            e.description,
            original,
            "→",
            _fmt_amount(c.new_cost),
            rate,
            src_tag,
        )

    console.print(table)
    console.print(_summary_panel(converted, total, target, by_source))


def _summary_panel(
    converted: list[ConvertedExpense],
    total: Decimal,
    target: CurrencyCode,
    by_source: dict[CurrencyCode, Decimal],
) -> Panel:
    pieces = Text("  +  ").join(
        Text(f"{_fmt_amount(amount)} {src}", style="amount.src")
        for src, amount in sorted(by_source.items())
    )

    n = len(converted)
    body = Text.assemble(
        ("Total: ", "heading"),
        (f"{_fmt_amount(total)} {target}", "amount.dst"),
        ("   ", ""),
        (f"({n} expense{'s' if n != 1 else ''})", "muted"),
        ("\nfrom:  ", "muted"),
        pieces,
    )
    return Panel(body, box=box.ROUNDED, padding=(0, 2), border_style="ok")


def render_cache_stats(console: Console, stats: CacheStats) -> None:
    line = Text.assemble(
        ("rates: ", "muted"),
        ("memory ", "stat.memory"),
        (f"{stats.memory_hits}", "muted"),
        ("  ", ""),
        ("disk ", "stat.disk"),
        (f"{stats.disk_hits}", "muted"),
        ("  ", ""),
        ("network ", "stat.network"),
        (f"{stats.network_hits}", "muted"),
    )
    console.print(line)


def make_fetch_status(console: Console, label: str) -> Status:
    """Indeterminate spinner for one-shot fetches (e.g. paginated expense list)."""
    return console.status(Text(label, style="info"), spinner="dots")


def make_step_progress(console: Console) -> Progress:
    """Determinate progress bar for known-count workloads (rate fetches, updates)."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(complete_style="green", finished_style="green"),
        MofNCompleteColumn(),
        TextColumn("•", style="muted"),
        TimeElapsedColumn(),
        TextColumn("•", style="muted"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )


def _fmt_amount(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01"))
    sign = "-" if quantized < 0 else ""
    abs_val = abs(quantized)
    int_part, _, frac_part = f"{abs_val:.2f}".partition(".")
    grouped = f"{int(int_part):,}"
    return f"{sign}{grouped}.{frac_part}"
