"""splitwise-fx CLI entry point."""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.prompt import Confirm
from rich.text import Text
from rich.traceback import install as install_rich_traceback

from . import __version__
from .convert import convert_expense, should_convert
from .display import (
    make_fetch_status,
    make_step_progress,
    render_cache_stats,
    render_preview,
)
from .models import (
    ConvertedExpense,
    CurrencyCode,
    Expense,
    Group,
    GroupId,
)
from .rates import (
    CachedRateProvider,
    RateUnavailableError,
)
from .splitwise import SplitwiseClient, SplitwiseError, build_update_payload
from .theme import make_console

WRITE_SLEEP_SECONDS = 1.0


def main() -> int:
    install_rich_traceback(show_locals=False)
    args = _parse_args()
    console = make_console()
    err_console = make_console(stderr=True)

    api_key = args.api_key or _load_api_key()
    if not api_key:
        _print_missing_api_key(err_console)
        return 2

    target = CurrencyCode(args.to.upper())

    try:
        with (
            SplitwiseClient(api_key) as sw,
            CachedRateProvider(no_cache=args.no_cache) as rates,
        ):
            group_id = _resolve_group(sw, args.group, console, err_console)
            if group_id is None:
                return 2

            with make_fetch_status(console, f"Fetching expenses for group {group_id}…"):
                expenses = list(
                    sw.iter_expenses(
                        group_id,
                        dated_after=args.after,
                        dated_before=args.before,
                    )
                )
            console.print(_summary_line(len(expenses), len(_filter(expenses, target))))

            converted, errors = _convert_all(
                _filter(expenses, target), target, rates, console, err_console
            )

            render_preview(console, converted, target=target)
            render_cache_stats(console, rates.stats)

            if errors:
                err_console.print(
                    Text(
                        f"{len(errors)} expense(s) failed conversion:",
                        style="error",
                    )
                )
                for expense_id, msg in errors:
                    err_console.print(
                        Text.assemble(
                            ("  ✗ ", "glyph.fail"),
                            (f"expense {expense_id}: ", ""),
                            (msg, "muted"),
                        )
                    )

            if not converted or args.dry_run:
                return 0 if not errors else 1

            if not args.yes and not Confirm.ask(
                Text.assemble(
                    "Update ",
                    (str(len(converted)), "heading"),
                    f" expense{'s' if len(converted) != 1 else ''} in Splitwise?",
                ),
                default=False,
                console=console,
            ):
                console.print(Text("Aborted.", style="warn"))
                return 0

            return _apply_updates(sw, converted, target, console, err_console)

    except SplitwiseError as exc:
        err_console.print(Text.assemble(("Splitwise error: ", "error"), str(exc)))
        return 1
    except RateUnavailableError as exc:
        err_console.print(Text.assemble(("Rate error: ", "error"), str(exc)))
        return 1


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="splitwise-fx",
        description="Bulk-convert Splitwise expenses to a target currency.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--group", required=True, help="Group name or numeric ID.")
    p.add_argument("--to", required=True, help="Target currency code (e.g. CNY).")
    p.add_argument("--after", type=_parse_date, help="Only expenses dated >= YYYY-MM-DD.")
    p.add_argument("--before", type=_parse_date, help="Only expenses dated <= YYYY-MM-DD.")
    p.add_argument("--dry-run", action="store_true", help="Preview only; do not write.")
    p.add_argument("--yes", action="store_true", help="Skip the y/N prompt.")
    p.add_argument(
        "--api-key",
        metavar="KEY",
        help=(
            "Splitwise personal API key. Overrides $SPLITWISE_API_KEY. "
            "Get one at https://secure.splitwise.com/apps. "
            "Note: passing it on the command line exposes it via `ps`; "
            "prefer the env var or a .env file."
        ),
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass on-disk rate cache (still writes back).",
    )
    return p.parse_args()


def _parse_date(text: str) -> date:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}") from exc


def _load_api_key() -> str | None:
    """Load SPLITWISE_API_KEY from env, optionally seeded by ./.env in cwd."""
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        load_dotenv(cwd_env)
    return os.environ.get("SPLITWISE_API_KEY")


def _print_missing_api_key(console: Console) -> None:
    console.print(Text("error: No Splitwise API key found.", style="error"))
    console.print()
    console.print(Text("1. Get a personal API key:", style="heading"))
    console.print(
        Text.assemble(
            ("      ", ""),
            ("https://secure.splitwise.com/apps", "info"),
            ("  (click ", "muted"),
            ("Register your application", ""),
            (")", "muted"),
        )
    )
    console.print()
    console.print(Text("2. Provide it one of three ways:", style="heading"))
    console.print()
    _print_api_key_option(
        console,
        "a)",
        "Environment variable (recommended):",
        ["export SPLITWISE_API_KEY=your-key-here", 'splitwise-fx --group "…" --to CNY'],
    )
    _print_api_key_option(
        console,
        "b)",
        ".env file in the directory you run from:",
        ["echo 'SPLITWISE_API_KEY=your-key-here' > .env"],
    )
    _print_api_key_option(
        console,
        "c)",
        "--api-key flag (visible in process list — least private):",
        ['splitwise-fx --api-key your-key-here --group "…" --to CNY'],
    )


def _print_api_key_option(console: Console, label: str, summary: str, examples: list[str]) -> None:
    console.print(Text.assemble(("   ", ""), (label + " ", "ok"), (summary, "")))
    for example in examples:
        console.print(Text(f"        {example}", style="muted"))
    console.print()


def _resolve_group(
    sw: SplitwiseClient, raw: str, console: Console, err_console: Console
) -> GroupId | None:
    if raw.isdigit():
        return GroupId(int(raw))

    groups = sw.list_groups()
    matches = [g for g in groups if g.name == raw]
    if not matches:
        matches = [g for g in groups if g.name.lower() == raw.lower()]
    if len(matches) == 1:
        return matches[0].id
    if not matches:
        err_console.print(Text.assemble(("error: ", "error"), (f"no group named {raw!r}.", "")))
        _list_groups(err_console, groups)
        return None
    err_console.print(
        Text.assemble(
            ("error: ", "error"),
            (f"ambiguous group name {raw!r}; matches {len(matches)} groups.", ""),
        )
    )
    _list_groups(err_console, matches)
    return None


def _list_groups(console: Console, groups: list[Group]) -> None:
    if not groups:
        return
    console.print(Text("Available groups:", style="muted"))
    for g in groups:
        console.print(
            Text.assemble(("  ", ""), (f"{int(g.id):>10}", "muted"), ("  ", ""), (g.name, ""))
        )


def _filter(expenses: list[Expense], target: CurrencyCode) -> list[Expense]:
    return [e for e in expenses if should_convert(e, target)]


def _summary_line(total: int, convertible: int) -> Text:
    return Text.assemble(
        (f"{total} expense{'s' if total != 1 else ''} in group, ", "muted"),
        (f"{convertible}", "heading"),
        (" to convert.", "muted"),
    )


def _convert_all(
    expenses: list[Expense],
    target: CurrencyCode,
    rates: CachedRateProvider,
    console: Console,
    err_console: Console,
) -> tuple[list[ConvertedExpense], list[tuple[int, str]]]:
    converted: list[ConvertedExpense] = []
    errors: list[tuple[int, str]] = []
    if not expenses:
        return converted, errors

    with make_step_progress(console) as progress:
        task = progress.add_task("Fetching rates", total=len(expenses))
        for expense in expenses:
            try:
                rate, source = rates.get_rate(
                    on=expense.date.date(),
                    src=expense.currency_code,
                    dst=target,
                )
                converted.append(convert_expense(expense, rate, target, rate_source=source))
            except (RateUnavailableError, ValueError) as exc:
                errors.append((int(expense.id), str(exc)))
                err_console.print(
                    Text.assemble(
                        ("  ! ", "glyph.skip"),
                        (f"skipping expense {int(expense.id)}: ", ""),
                        (str(exc), "muted"),
                    )
                )
            progress.advance(task)
    return converted, errors


def _apply_updates(
    sw: SplitwiseClient,
    converted: list[ConvertedExpense],
    target: CurrencyCode,
    console: Console,
    err_console: Console,
) -> int:
    failed = 0
    with make_step_progress(console) as progress:
        task = progress.add_task("Updating expenses", total=len(converted))
        for i, c in enumerate(converted):
            if i > 0:
                time.sleep(WRITE_SLEEP_SECONDS)
            payload = build_update_payload(c.expense, c, target)
            try:
                sw.update_expense(c.expense.id, payload)
                progress.console.print(
                    Text.assemble(
                        ("  ✓ ", "glyph.ok"),
                        (f"{int(c.expense.id)}  ", "muted"),
                        (c.expense.description, ""),
                    )
                )
            except SplitwiseError as exc:
                failed += 1
                err_console.print(
                    Text.assemble(
                        ("  ✗ ", "glyph.fail"),
                        (f"{int(c.expense.id)}  ", "muted"),
                        (c.expense.description, ""),
                        (" — ", "muted"),
                        (str(exc), "muted"),
                    )
                )
            progress.advance(task)

    if failed:
        err_console.print(Text(f"{failed} update(s) failed.", style="error"))
        return 1
    console.print(Text(f"Updated {len(converted)} expense(s).", style="ok"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
