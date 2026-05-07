"""Tests for splitwise.py — pagination, payload shape, retry."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from splitwise_fx.models import (
    ConvertedExpense,
    ConvertedShare,
    CurrencyCode,
    Expense,
    ExpenseId,
    ExpenseUser,
    GroupId,
    UserId,
)
from splitwise_fx.splitwise import (
    BASE_URL,
    PAGE_SIZE,
    SplitwiseClient,
    build_update_payload,
)


def _expense_dict(expense_id: int, *, deleted: bool = False) -> dict[str, object]:
    return {
        "id": expense_id,
        "description": f"e{expense_id}",
        "cost": "100.0",
        "currency_code": "JPY",
        "date": "2026-05-01T00:00:00Z",
        "group_id": 1,
        "category_id": 18,
        "payment": False,
        "deleted_at": "2026-05-02T00:00:00Z" if deleted else None,
        "users": [
            {
                "user_id": 10,
                "paid_share": "100.0",
                "owed_share": "50.0",
                "user": {"id": 10, "first_name": "A", "last_name": "B"},
            },
            {
                "user_id": 11,
                "paid_share": "0.0",
                "owed_share": "50.0",
                "user": {"id": 11, "first_name": "C", "last_name": "D"},
            },
        ],
    }


def test_iter_expenses_paginates_until_short_page() -> None:
    """Two full pages then a short page → stop."""
    full_page = {"expenses": [_expense_dict(i) for i in range(PAGE_SIZE)]}
    short_page = {"expenses": [_expense_dict(i) for i in range(PAGE_SIZE, PAGE_SIZE + 5)]}

    with respx.mock(base_url=BASE_URL, assert_all_called=True) as router:
        # offset=0 and offset=PAGE_SIZE return full pages; offset=2*PAGE_SIZE returns short.
        page1 = router.get(re.compile(r".*/get_expenses\?.*offset=0\b.*")).respond(
            200, json=full_page
        )
        page2 = router.get(re.compile(rf".*/get_expenses\?.*offset={PAGE_SIZE}\b.*")).respond(
            200, json=full_page
        )
        page3 = router.get(re.compile(rf".*/get_expenses\?.*offset={2 * PAGE_SIZE}\b.*")).respond(
            200, json=short_page
        )

        with SplitwiseClient("test-key") as sw:
            results = list(sw.iter_expenses(GroupId(1)))

    assert len(results) == 2 * PAGE_SIZE + 5
    assert page1.called and page2.called and page3.called


def test_iter_expenses_skips_deleted() -> None:
    page = {
        "expenses": [
            _expense_dict(1),
            _expense_dict(2, deleted=True),
            _expense_dict(3),
        ]
    }
    with respx.mock(base_url=BASE_URL) as router:
        router.get(re.compile(r".*/get_expenses.*")).respond(200, json=page)
        with SplitwiseClient("test-key") as sw:
            results = list(sw.iter_expenses(GroupId(1)))
    assert [int(e.id) for e in results] == [1, 3]


def test_list_groups_parses_response() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/get_groups").respond(
            200,
            json={"groups": [{"id": 1, "name": "Tokyo"}, {"id": 2, "name": "NZ"}]},
        )
        with SplitwiseClient("test-key") as sw:
            groups = sw.list_groups()
    assert [(int(g.id), g.name) for g in groups] == [(1, "Tokyo"), (2, "NZ")]


def test_update_expense_posts_payload() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"expenses": []})

    with respx.mock(base_url=BASE_URL) as router:
        router.post("/update_expense/42").mock(side_effect=handler)
        with SplitwiseClient("test-key") as sw:
            sw.update_expense(ExpenseId(42), {"cost": "10.00"})

    assert captured == [{"cost": "10.00"}]


def test_request_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """First two responses are 429, third is 200."""
    monkeypatch.setattr("splitwise_fx.splitwise.time.sleep", lambda _s: None)

    with respx.mock(base_url=BASE_URL) as router:
        route = router.get("/get_groups").mock(
            side_effect=[
                httpx.Response(429),
                httpx.Response(429),
                httpx.Response(200, json={"groups": []}),
            ]
        )
        with SplitwiseClient("test-key") as sw:
            sw.list_groups()
    assert route.call_count == 3


# ---------------------------------------------------------------------------
# build_update_payload
# ---------------------------------------------------------------------------


def make_expense(*, group_id: int = 1, category_id: int | None = 18) -> Expense:
    return Expense(
        id=ExpenseId(1),
        description="Sushi",
        cost="15000.0",
        currency_code=CurrencyCode("JPY"),
        date=datetime(2026, 5, 1, tzinfo=UTC),
        group_id=GroupId(group_id),
        category_id=category_id,
        payment=False,
        users=[
            ExpenseUser(user_id=UserId(10), paid_share="15000.0", owed_share="7500.0"),
            ExpenseUser(user_id=UserId(11), paid_share="0.0", owed_share="7500.0"),
        ],
    )


def make_converted(expense: Expense) -> ConvertedExpense:
    return ConvertedExpense(
        expense=expense,
        target_currency=CurrencyCode("CNY"),
        rate=Decimal("0.04523"),
        rate_source="unionpay",
        new_cost=Decimal("678.45"),
        shares=[
            ConvertedShare(
                user_id=UserId(10),
                old_paid=Decimal("15000.0"),
                old_owed=Decimal("7500.0"),
                new_paid=Decimal("678.45"),
                new_owed=Decimal("339.22"),
            ),
            ConvertedShare(
                user_id=UserId(11),
                old_paid=Decimal("0.0"),
                old_owed=Decimal("7500.0"),
                new_paid=Decimal("0.00"),
                new_owed=Decimal("339.23"),
            ),
        ],
    )


def test_build_update_payload_uses_flattened_user_format() -> None:
    expense = make_expense()
    converted = make_converted(expense)

    payload = build_update_payload(expense, converted, CurrencyCode("CNY"))

    assert payload["cost"] == "678.45"
    assert payload["currency_code"] == "CNY"
    assert payload["description"] == "Sushi"
    assert payload["group_id"] == 1
    assert payload["category_id"] == 18
    date_value = payload["date"]
    assert isinstance(date_value, str) and date_value.endswith("Z")

    assert payload["users__0__user_id"] == 10
    assert payload["users__0__paid_share"] == "678.45"
    assert payload["users__0__owed_share"] == "339.22"

    assert payload["users__1__user_id"] == 11
    assert payload["users__1__paid_share"] == "0.00"
    assert payload["users__1__owed_share"] == "339.23"

    # Sums must match cost — invariant Splitwise enforces.
    paid_sum = sum(
        Decimal(str(payload[f"users__{i}__paid_share"])) for i in range(len(converted.shares))
    )
    owed_sum = sum(
        Decimal(str(payload[f"users__{i}__owed_share"])) for i in range(len(converted.shares))
    )
    assert paid_sum == Decimal("678.45")
    assert owed_sum == Decimal("678.45")


def test_build_update_payload_omits_category_when_none() -> None:
    expense = make_expense(category_id=None)
    converted = make_converted(expense)
    payload = build_update_payload(expense, converted, CurrencyCode("CNY"))
    assert "category_id" not in payload
