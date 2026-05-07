"""Splitwise REST API client (sync httpx)."""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from datetime import date
from decimal import Decimal
from typing import Final, cast

import httpx

from .models import (
    ConvertedExpense,
    CurrencyCode,
    Expense,
    ExpenseId,
    GetExpensesResponse,
    GetGroupsResponse,
    Group,
    GroupId,
)

BASE_URL: Final = "https://secure.splitwise.com/api/v3.0"
PAGE_SIZE: Final = 100
MAX_RETRIES: Final = 5

# Splitwise's update_expense payload mixes str (cost, share, description, ISO date) and
# int (user_id, group_id, category_id). No Decimals or floats — values that look numeric
# are passed as strings to preserve trailing zeros.
UpdatePayload = Mapping[str, str | int]


class SplitwiseError(RuntimeError):
    """Raised when the Splitwise API returns a non-success response."""


class SplitwiseClient:
    def __init__(self, api_key: str, *, timeout: float = 30.0) -> None:
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "splitwise-fx/0.1",
            },
            timeout=timeout,
        )

    def __enter__(self) -> SplitwiseClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json: UpdatePayload | None = None,
    ) -> dict[str, object]:
        backoff = 1.0
        last_status = 0
        for _ in range(MAX_RETRIES):
            resp = self._http.request(method, path, params=params, json=json)
            last_status = resp.status_code
            if resp.status_code == 429:
                time.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code >= 500:
                time.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code >= 400:
                raise SplitwiseError(f"{method} {path} → {resp.status_code}: {resp.text[:300]}")
            return cast("dict[str, object]", resp.json())
        raise SplitwiseError(
            f"{method} {path} gave up after {MAX_RETRIES} retries (last={last_status})"
        )

    def list_groups(self) -> list[Group]:
        data = self._request("GET", "/get_groups")
        return GetGroupsResponse.model_validate(data).groups

    def iter_expenses(
        self,
        group_id: GroupId,
        *,
        dated_after: date | None = None,
        dated_before: date | None = None,
    ) -> Iterator[Expense]:
        offset = 0
        while True:
            params: dict[str, str | int] = {
                "group_id": int(group_id),
                "limit": PAGE_SIZE,
                "offset": offset,
            }
            if dated_after is not None:
                params["dated_after"] = dated_after.isoformat()
            if dated_before is not None:
                params["dated_before"] = dated_before.isoformat()
            data = self._request("GET", "/get_expenses", params=params)
            page = GetExpensesResponse.model_validate(data).expenses
            for expense in page:
                if expense.deleted_at is not None:
                    continue
                yield expense
            if len(page) < PAGE_SIZE:
                return
            offset += PAGE_SIZE

    def update_expense(self, expense_id: ExpenseId, payload: UpdatePayload) -> None:
        self._request("POST", f"/update_expense/{int(expense_id)}", json=payload)


def build_update_payload(
    original: Expense,
    converted: ConvertedExpense,
    target_currency: CurrencyCode,
) -> dict[str, str | int]:
    """Build the flattened-user payload Splitwise's update_expense expects."""

    if original.group_id is None:
        raise ValueError(f"expense {original.id} has no group_id; cannot update")

    payload: dict[str, str | int] = {
        "cost": _money(converted.new_cost),
        "description": original.description,
        "currency_code": str(target_currency),
        "group_id": int(original.group_id),
        "date": original.date.isoformat().replace("+00:00", "Z"),
    }
    if original.category_id is not None:
        payload["category_id"] = original.category_id

    for i, share in enumerate(converted.shares):
        payload[f"users__{i}__user_id"] = int(share.user_id)
        payload[f"users__{i}__paid_share"] = _money(share.new_paid)
        payload[f"users__{i}__owed_share"] = _money(share.new_owed)

    return payload


def _money(value: Decimal) -> str:
    """Stringify a Decimal with exactly 2 decimal places (Splitwise requirement)."""
    return f"{value:.2f}"
