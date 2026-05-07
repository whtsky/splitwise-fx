"""Typed models — pydantic for IO boundaries, dataclasses for internal types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import NewType

from pydantic import BaseModel, ConfigDict, Field

CurrencyCode = NewType("CurrencyCode", str)
ExpenseId = NewType("ExpenseId", int)
UserId = NewType("UserId", int)
GroupId = NewType("GroupId", int)


class _ApiModel(BaseModel):
    """Tolerant base — Splitwise responses contain many fields we don't model."""

    model_config = ConfigDict(extra="ignore")


class ExpenseUserRef(_ApiModel):
    """Nested `user` object inside an expense's `users` array."""

    id: UserId
    first_name: str | None = None
    last_name: str | None = None


class ExpenseUser(_ApiModel):
    """One user's stake in an expense."""

    user_id: UserId
    paid_share: str
    owed_share: str
    user: ExpenseUserRef | None = None


class Expense(_ApiModel):
    """A single Splitwise expense as returned by /get_expenses."""

    id: ExpenseId
    description: str
    cost: str
    currency_code: CurrencyCode
    date: datetime
    group_id: GroupId | None = None
    category_id: int | None = Field(default=None)
    payment: bool = False
    deleted_at: datetime | None = None
    users: list[ExpenseUser] = Field(default_factory=list)


class Group(_ApiModel):
    id: GroupId
    name: str


class GetGroupsResponse(_ApiModel):
    groups: list[Group]


class GetExpensesResponse(_ApiModel):
    expenses: list[Expense]


class UnionPayRateRow(_ApiModel):
    transCur: str
    baseCur: str
    rateData: Decimal


class UnionPayResponse(_ApiModel):
    curDate: str
    exchangeRateJson: list[UnionPayRateRow]


class FrankfurterRate(_ApiModel):
    """Response shape of GET https://api.frankfurter.dev/v2/rate/{base}/{quote}."""

    date: str
    base: str
    quote: str
    rate: Decimal


@dataclass(frozen=True, slots=True)
class ConvertedShare:
    user_id: UserId
    old_paid: Decimal
    old_owed: Decimal
    new_paid: Decimal
    new_owed: Decimal


@dataclass(frozen=True, slots=True)
class ConvertedExpense:
    """Result of converting one expense — both old and new values for audit."""

    expense: Expense
    target_currency: CurrencyCode
    rate: Decimal
    rate_source: str  # "unionpay" | "frankfurter"
    new_cost: Decimal
    shares: list[ConvertedShare]


@dataclass(frozen=True, slots=True)
class CacheStats:
    memory_hits: int = 0
    disk_hits: int = 0
    network_hits: int = 0

    def add(self, *, memory: int = 0, disk: int = 0, network: int = 0) -> CacheStats:
        return CacheStats(
            memory_hits=self.memory_hits + memory,
            disk_hits=self.disk_hits + disk,
            network_hits=self.network_hits + network,
        )
