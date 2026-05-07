"""Tests for convert.py — Decimal math + share-sum fix-up."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from splitwise_fx.convert import _distribute_residual, convert_expense, should_convert
from splitwise_fx.models import (
    CurrencyCode,
    Expense,
    ExpenseId,
    ExpenseUser,
    GroupId,
    UserId,
)

CNY = CurrencyCode("CNY")
JPY = CurrencyCode("JPY")
USD = CurrencyCode("USD")


def make_expense(
    *,
    cost: str,
    currency: CurrencyCode,
    users: list[tuple[int, str, str]],
    payment: bool = False,
) -> Expense:
    return Expense(
        id=ExpenseId(1),
        description="t",
        cost=cost,
        currency_code=currency,
        date=datetime(2026, 5, 1, tzinfo=UTC),
        group_id=GroupId(123),
        category_id=15,
        payment=payment,
        users=[ExpenseUser(user_id=UserId(uid), paid_share=p, owed_share=o) for uid, p, o in users],
    )


# ---------------------------------------------------------------------------
# should_convert
# ---------------------------------------------------------------------------


def test_should_convert_skips_same_currency() -> None:
    e = make_expense(cost="100", currency=CNY, users=[(1, "100", "100")])
    assert should_convert(e, CNY) is False


def test_should_convert_skips_payment() -> None:
    e = make_expense(cost="100", currency=JPY, users=[(1, "100", "100")], payment=True)
    assert should_convert(e, CNY) is False


def test_should_convert_skips_zero_cost() -> None:
    e = make_expense(cost="0.0", currency=JPY, users=[(1, "0", "0")])
    assert should_convert(e, CNY) is False


def test_should_convert_accepts_foreign() -> None:
    e = make_expense(cost="100", currency=JPY, users=[(1, "100", "100")])
    assert should_convert(e, CNY) is True


# ---------------------------------------------------------------------------
# Share-sum invariant — the load-bearing test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cost,rate,users",
    [
        # 50/50 split, ¥15000 JPY at 0.04523 → 678.45 CNY;
        # 339.225 each rounds up to 339.23 (residual -0.01).
        ("15000.0", Decimal("0.04523"), [(1, "15000", "7500"), (2, "0", "7500")]),
        # 3-way split with non-trivial rounding
        ("100.0", Decimal("7.123"), [(1, "100", "33.34"), (2, "0", "33.33"), (3, "0", "33.33")]),
        # 4-way split
        ("80.0", Decimal("7.0"), [(1, "80", "20"), (2, "0", "20"), (3, "0", "20"), (4, "0", "20")]),
        # Rate that creates over-shoot
        ("13.0", Decimal("0.071"), [(1, "13", "6.5"), (2, "0", "6.5")]),
        # Rate that creates under-shoot
        ("11.0", Decimal("0.069"), [(1, "11", "5.5"), (2, "0", "5.5")]),
    ],
)
def test_share_sum_invariant(cost: str, rate: Decimal, users: list[tuple[int, str, str]]) -> None:
    e = make_expense(cost=cost, currency=JPY, users=users)
    converted = convert_expense(e, rate, CNY, rate_source="unionpay")

    paid_sum = sum((s.new_paid for s in converted.shares), Decimal(0))
    owed_sum = sum((s.new_owed for s in converted.shares), Decimal(0))

    assert paid_sum == converted.new_cost, f"paid_sum {paid_sum} != cost {converted.new_cost}"
    assert owed_sum == converted.new_cost, f"owed_sum {owed_sum} != cost {converted.new_cost}"
    # No share goes negative
    for s in converted.shares:
        assert s.new_paid >= 0
        assert s.new_owed >= 0


def test_zero_paid_share_stays_zero() -> None:
    """A user who didn't pay should not gain a paid_share through fix-up."""
    e = make_expense(cost="100", currency=JPY, users=[(1, "100", "50"), (2, "0", "50")])
    c = convert_expense(e, Decimal("7.123"), CNY, rate_source="unionpay")
    user2 = next(s for s in c.shares if int(s.user_id) == 2)
    assert user2.new_paid == Decimal("0.00")


def test_currency_and_rate_metadata_preserved() -> None:
    e = make_expense(cost="100", currency=JPY, users=[(1, "100", "100")])
    c = convert_expense(e, Decimal("0.04523"), CNY, rate_source="unionpay")
    assert c.target_currency == CNY
    assert c.rate == Decimal("0.04523")
    assert c.rate_source == "unionpay"


# ---------------------------------------------------------------------------
# _distribute_residual edge cases
# ---------------------------------------------------------------------------


def test_distribute_residual_no_op_when_balanced() -> None:
    values = [Decimal("10.00"), Decimal("10.00")]
    assert _distribute_residual(values, Decimal("20.00")) == values


def test_distribute_residual_adds_cent_to_largest() -> None:
    values = [Decimal("10.01"), Decimal("9.99")]
    out = _distribute_residual(values, Decimal("20.01"))
    assert sum(out, Decimal(0)) == Decimal("20.01")
    assert out[0] == Decimal("10.02")


def test_distribute_residual_subtracts_cent_from_largest() -> None:
    values = [Decimal("10.01"), Decimal("9.99")]
    out = _distribute_residual(values, Decimal("19.99"))
    assert sum(out, Decimal(0)) == Decimal("19.99")


def test_distribute_residual_skips_zero_entries() -> None:
    """Zero entries (didn't pay) must not absorb residuals."""
    values = [Decimal("10.00"), Decimal("0.00")]
    out = _distribute_residual(values, Decimal("10.01"))
    assert out[1] == Decimal("0.00")
    assert out[0] == Decimal("10.01")


def test_distribute_residual_raises_when_all_zero() -> None:
    with pytest.raises(ValueError, match="all share values are zero"):
        _distribute_residual([Decimal("0"), Decimal("0")], Decimal("0.01"))
