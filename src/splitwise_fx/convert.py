"""Pure conversion logic — Decimal math + share-sum fix-up.

The Splitwise API rejects updates where ``sum(paid_share) != cost`` or
``sum(owed_share) != cost``. After per-share rounding the sum can drift by
±0.01·N from the target; ``_distribute_residual`` fixes that drift by stepping
the largest-magnitude non-zero shares.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from .models import (
    ConvertedExpense,
    ConvertedShare,
    CurrencyCode,
    Expense,
)

CENT: Final = Decimal("0.01")


def should_convert(expense: Expense, target: CurrencyCode) -> bool:
    """Skip rules: same-currency, settle-up payments, zero-cost edge cases."""
    if expense.currency_code == target:
        return False
    if expense.payment:
        return False
    return Decimal(expense.cost) != 0


def convert_expense(
    expense: Expense,
    rate: Decimal,
    target: CurrencyCode,
    *,
    rate_source: str,
) -> ConvertedExpense:
    """Convert one expense and return both old + new values for audit."""

    new_cost = _round_money(Decimal(expense.cost) * rate)

    raw_paid = [_round_money(Decimal(u.paid_share) * rate) for u in expense.users]
    raw_owed = [_round_money(Decimal(u.owed_share) * rate) for u in expense.users]

    fixed_paid = _distribute_residual(raw_paid, new_cost)
    fixed_owed = _distribute_residual(raw_owed, new_cost)

    shares = [
        ConvertedShare(
            user_id=u.user_id,
            old_paid=Decimal(u.paid_share),
            old_owed=Decimal(u.owed_share),
            new_paid=fixed_paid[i],
            new_owed=fixed_owed[i],
        )
        for i, u in enumerate(expense.users)
    ]

    return ConvertedExpense(
        expense=expense,
        target_currency=target,
        rate=rate,
        rate_source=rate_source,
        new_cost=new_cost,
        shares=shares,
    )


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _distribute_residual(values: list[Decimal], target_sum: Decimal) -> list[Decimal]:
    """Adjust ``values`` by ±0.01 increments so they sum to ``target_sum`` exactly.

    Steps are applied to the largest non-zero entries first; zero entries stay
    zero (a user with no stake in the expense should not gain one through a
    rounding fix-up). Raises if there's no entry large enough to absorb a
    negative step without going below zero.
    """

    residual = target_sum - sum(values, Decimal(0))
    if residual == 0:
        return list(values)

    step = CENT if residual > 0 else -CENT
    n_steps = int((abs(residual) / CENT).to_integral_value())

    candidates = [i for i, v in enumerate(values) if v > 0]
    if not candidates:
        raise ValueError(f"cannot absorb residual {residual} — all share values are zero")
    candidates.sort(key=lambda i: (-values[i], i))

    out = list(values)
    for k in range(n_steps):
        # Try candidates in rank order, looking for one that won't go negative.
        for offset in range(len(candidates)):
            idx = candidates[(k + offset) % len(candidates)]
            if out[idx] + step >= 0:
                out[idx] += step
                break
        else:
            raise ValueError(f"cannot absorb residual {residual} without taking a share below zero")
    return out
