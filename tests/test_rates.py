"""Tests for rates.py — provider, cache layers, fallback."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TypedDict, cast

import pytest
import respx

from splitwise_fx.models import CurrencyCode
from splitwise_fx.rates import (
    FRANKFURTER_URL,
    UNIONPAY_URL,
    CachedRateProvider,
    RateUnavailableError,
)

JPY = CurrencyCode("JPY")
CNY = CurrencyCode("CNY")
USD = CurrencyCode("USD")
KRW = CurrencyCode("KRW")  # not in UNIONPAY_BASES — forces Frankfurter for `--to KRW`

FIXTURE = Path(__file__).parent / "fixtures" / "unionpay_20260506.json"


class _UnionPayRow(TypedDict):
    transCur: str
    baseCur: str
    rateData: float


class _UnionPayPayload(TypedDict):
    curDate: str
    exchangeRateJson: list[_UnionPayRow]


def _unionpay_url(d: date) -> str:
    return UNIONPAY_URL.format(date=d.strftime("%Y%m%d"))


def _frankfurter_url(src: str, dst: str) -> str:
    return FRANKFURTER_URL.format(src=src, dst=dst)


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


@pytest.fixture
def fixture_payload() -> _UnionPayPayload:
    return cast("_UnionPayPayload", json.loads(FIXTURE.read_text()))


def test_unionpay_lookup_jpy_to_cny(cache_dir: Path, fixture_payload: _UnionPayPayload) -> None:
    on = date(2026, 5, 6)
    with respx.mock(assert_all_called=True) as router:
        router.get(_unionpay_url(on)).respond(200, json=fixture_payload)
        with CachedRateProvider(cache_dir=cache_dir) as p:
            rate, source = p.get_rate(on, JPY, CNY)
    assert source == "unionpay"
    assert rate == Decimal("0.043511")


def test_unionpay_lookup_jpy_to_usd(cache_dir: Path, fixture_payload: _UnionPayPayload) -> None:
    on = date(2026, 5, 6)
    with respx.mock(assert_all_called=True) as router:
        router.get(_unionpay_url(on)).respond(200, json=fixture_payload)
        with CachedRateProvider(cache_dir=cache_dir) as p:
            rate, source = p.get_rate(on, JPY, USD)
    assert source == "unionpay"
    assert rate == Decimal("0.006353")


def test_memory_cache_avoids_network(cache_dir: Path, fixture_payload: _UnionPayPayload) -> None:
    on = date(2026, 5, 6)
    with respx.mock(assert_all_called=True) as router:
        route = router.get(_unionpay_url(on)).respond(200, json=fixture_payload)
        with CachedRateProvider(cache_dir=cache_dir) as p:
            p.get_rate(on, JPY, CNY)
            p.get_rate(on, JPY, CNY)
            p.get_rate(on, JPY, CNY)
            stats = p.stats
    assert route.call_count == 1, "second + third lookups should hit memory, not network"
    # 3 calls: 1 hits network (and adds to memory), 2 hit the per-pair memo.
    assert stats.network_hits == 1
    assert stats.memory_hits == 2


def test_disk_cache_persists_across_provider_instances(
    cache_dir: Path, fixture_payload: _UnionPayPayload
) -> None:
    on = date(2026, 5, 6)
    # First provider — fetches and writes to disk.
    with respx.mock(assert_all_called=True) as router:
        router.get(_unionpay_url(on)).respond(200, json=fixture_payload)
        with CachedRateProvider(cache_dir=cache_dir) as p1:
            p1.get_rate(on, JPY, CNY)

    # Second provider — should hit disk, NOT make a network call.
    with respx.mock(assert_all_called=False) as router:
        route = router.get(_unionpay_url(on)).respond(200, json=fixture_payload)
        with CachedRateProvider(cache_dir=cache_dir) as p2:
            rate, source = p2.get_rate(on, JPY, CNY)
            stats = p2.stats
    assert route.call_count == 0, "second instance should hit disk, not network"
    assert source == "unionpay"
    assert rate == Decimal("0.043511")
    assert stats.disk_hits == 1
    assert stats.network_hits == 0


def test_no_cache_flag_bypasses_disk(cache_dir: Path, fixture_payload: _UnionPayPayload) -> None:
    on = date(2026, 5, 6)
    # Pre-warm disk cache.
    with respx.mock() as router:
        router.get(_unionpay_url(on)).respond(200, json=fixture_payload)
        with CachedRateProvider(cache_dir=cache_dir) as p1:
            p1.get_rate(on, JPY, CNY)

    # With --no-cache, disk is ignored — must hit network again.
    with respx.mock(assert_all_called=True) as router:
        route = router.get(_unionpay_url(on)).respond(200, json=fixture_payload)
        with CachedRateProvider(cache_dir=cache_dir, no_cache=True) as p2:
            p2.get_rate(on, JPY, CNY)
    assert route.call_count == 1


def test_frankfurter_fallback_when_target_not_in_unionpay_bases(cache_dir: Path) -> None:
    """KRW is not a UnionPay base currency → must hit Frankfurter."""
    on = date(2026, 5, 6)
    with respx.mock(assert_all_called=True) as router:
        route = router.get(_frankfurter_url("USD", "KRW")).respond(
            200,
            json={"date": "2026-05-06", "base": "USD", "quote": "KRW", "rate": 1310.5},
        )
        with CachedRateProvider(cache_dir=cache_dir) as p:
            rate, source = p.get_rate(on, USD, KRW)
    assert route.call_count == 1
    assert source == "frankfurter"
    assert rate == Decimal("1310.5")


def test_frankfurter_fallback_when_pair_missing_from_unionpay(
    cache_dir: Path, fixture_payload: _UnionPayPayload
) -> None:
    """If UnionPay file lacks the pair (e.g. exotic src), fall back to Frankfurter."""
    on = date(2026, 5, 6)
    # Remove JPY rows from fixture to force fallback.
    payload: _UnionPayPayload = {
        "curDate": fixture_payload["curDate"],
        "exchangeRateJson": [
            r for r in fixture_payload["exchangeRateJson"] if r["transCur"] != "JPY"
        ],
    }
    with respx.mock(assert_all_called=True) as router:
        router.get(_unionpay_url(on)).respond(200, json=payload)
        router.get(_frankfurter_url("JPY", "CNY")).respond(
            200,
            json={"date": "2026-05-06", "base": "JPY", "quote": "CNY", "rate": 0.043511},
        )
        with CachedRateProvider(cache_dir=cache_dir) as p:
            rate, source = p.get_rate(on, JPY, CNY)
    assert source == "frankfurter"
    assert rate == Decimal("0.043511")


def test_unionpay_404_falls_back_through_recent_days(
    cache_dir: Path, fixture_payload: _UnionPayPayload
) -> None:
    """If today's UnionPay file 404s, walk back day-by-day."""
    on = date(2026, 5, 6)
    yesterday = date(2026, 5, 5)
    with respx.mock(assert_all_called=True) as router:
        router.get(_unionpay_url(on)).respond(404)
        router.get(_unionpay_url(yesterday)).respond(200, json=fixture_payload)
        with CachedRateProvider(cache_dir=cache_dir) as p:
            rate, source = p.get_rate(on, JPY, CNY)
    assert source == "unionpay"
    assert rate == Decimal("0.043511")


def test_future_date_raises_immediately(cache_dir: Path) -> None:
    on = date(2099, 1, 1)
    with respx.mock() as router:
        # No routes — provider must not call anything.
        with (
            CachedRateProvider(cache_dir=cache_dir) as p,
            pytest.raises(RateUnavailableError, match="future-dated"),
        ):
            p.get_rate(on, JPY, CNY)
        assert len(router.calls) == 0


def test_completely_unavailable_raises(cache_dir: Path) -> None:
    on = date(2026, 5, 6)
    with respx.mock() as router:
        # walk-back covers `on` itself + UNIONPAY_FALLBACK_DAYS-1 prior days
        for offset in range(7):
            router.get(_unionpay_url(on - timedelta(days=offset))).respond(404)
        router.get(_frankfurter_url("JPY", "CNY")).respond(500)
        with CachedRateProvider(cache_dir=cache_dir) as p, pytest.raises(RateUnavailableError):
            p.get_rate(on, JPY, CNY)


def test_atomic_write_leaves_no_tmp_file(
    cache_dir: Path, fixture_payload: _UnionPayPayload
) -> None:
    on = date(2026, 5, 6)
    with respx.mock() as router:
        router.get(_unionpay_url(on)).respond(200, json=fixture_payload)
        with CachedRateProvider(cache_dir=cache_dir) as p:
            p.get_rate(on, JPY, CNY)
    # No .tmp leftovers
    leftovers = list((cache_dir / "unionpay").glob("*.tmp"))
    assert leftovers == []
    # But the real file is there
    assert (cache_dir / "unionpay" / "20260506.json").exists()


def test_corrupt_disk_cache_falls_back_to_network(
    cache_dir: Path, fixture_payload: _UnionPayPayload
) -> None:
    on = date(2026, 5, 6)
    # Pre-write garbage
    (cache_dir / "unionpay").mkdir(parents=True, exist_ok=True)
    (cache_dir / "unionpay" / "20260506.json").write_text("not-json{{{")

    with respx.mock(assert_all_called=True) as router:
        route = router.get(_unionpay_url(on)).respond(200, json=fixture_payload)
        with CachedRateProvider(cache_dir=cache_dir) as p:
            rate, _ = p.get_rate(on, JPY, CNY)
    assert route.call_count == 1
    assert rate == Decimal("0.043511")
