"""Currency rate provider — UnionPay primary, Frankfurter fallback, two-layer cache."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final, Protocol

import httpx

from . import __version__
from .models import (
    CacheStats,
    CurrencyCode,
    FrankfurterRate,
    UnionPayResponse,
)

UNIONPAY_BASES: Final[frozenset[str]] = frozenset(
    {"AUD", "CAD", "CNY", "EUR", "GBP", "HKD", "HUF", "JPY",
     "MNT", "MOP", "NZD", "SGD", "THB", "USD", "VND"}
)  # fmt: skip

UNIONPAY_URL: Final = "https://www.unionpayintl.com/upload/jfimg/{date}.json"
FRANKFURTER_URL: Final = "https://api.frankfurter.dev/v2/rate/{src}/{dst}"
USER_AGENT: Final = f"splitwise-fx/{__version__} (+https://github.com/whtsky/splitwise-fx)"

# Number of days BEFORE `on` to try in the walkback. Total attempts = this + 1
# (the requested date itself, then up to N prior days).
UNIONPAY_LOOKBACK_DAYS: Final = 7
TODAY_TTL: Final = timedelta(hours=1)


class RateUnavailableError(RuntimeError):
    """Raised when neither UnionPay nor Frankfurter can supply a rate."""


class RateProvider(Protocol):
    def get_rate(self, on: date, src: CurrencyCode, dst: CurrencyCode) -> tuple[Decimal, str]:
        """Return (rate, source) where source is "unionpay" or "frankfurter"."""
        ...

    @property
    def stats(self) -> CacheStats: ...


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "splitwise-fx"


class CachedRateProvider:
    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        no_cache: bool = False,
        http: httpx.Client | None = None,
    ) -> None:
        self._cache_dir = cache_dir or default_cache_dir()
        self._no_cache = no_cache
        self._http = http or httpx.Client(
            timeout=20.0,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        self._owns_http = http is None
        self._mem_unionpay: dict[date, UnionPayResponse] = {}
        self._mem_pair: dict[tuple[date, str, str], tuple[Decimal, str]] = {}
        self._stats = CacheStats()

        (self._cache_dir / "unionpay").mkdir(parents=True, exist_ok=True)
        (self._cache_dir / "frankfurter").mkdir(parents=True, exist_ok=True)

    def __enter__(self) -> CachedRateProvider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    @property
    def stats(self) -> CacheStats:
        return self._stats

    def get_rate(self, on: date, src: CurrencyCode, dst: CurrencyCode) -> tuple[Decimal, str]:
        if on > _today_utc():
            raise RateUnavailableError(f"refusing to fetch future-dated rate: {on}")

        memo_key = (on, str(src), str(dst))
        if (cached := self._mem_pair.get(memo_key)) is not None:
            self._stats = self._stats.add(memory=1)
            return cached

        if dst in UNIONPAY_BASES:
            rate = self._try_unionpay(on, src, dst)
            if rate is not None:
                result = (rate, "unionpay")
                self._mem_pair[memo_key] = result
                return result

        rate = self._try_frankfurter(on, src, dst)
        if rate is not None:
            result = (rate, "frankfurter")
            self._mem_pair[memo_key] = result
            return result

        raise RateUnavailableError(f"no rate available for {src}->{dst} on {on}")

    # ---- UnionPay -----------------------------------------------------------

    def _try_unionpay(self, on: date, src: CurrencyCode, dst: CurrencyCode) -> Decimal | None:
        # Try `on` itself, then up to UNIONPAY_LOOKBACK_DAYS prior days.
        for offset in range(UNIONPAY_LOOKBACK_DAYS + 1):
            day = on - timedelta(days=offset)
            response = self._load_unionpay(day)
            if response is None:
                continue
            for row in response.exchangeRateJson:
                if row.transCur == src and row.baseCur == dst:
                    return row.rateData
            # Day's file exists but lacks this pair → walking back won't help.
            return None
        return None

    def _load_unionpay(self, on: date) -> UnionPayResponse | None:
        if (cached := self._mem_unionpay.get(on)) is not None:
            self._stats = self._stats.add(memory=1)
            return cached

        path = self._cache_dir / "unionpay" / f"{_yyyymmdd(on)}.json"
        if not self._no_cache and self._is_fresh(path, on):
            try:
                payload = json.loads(path.read_bytes())
                response = UnionPayResponse.model_validate(payload)
                self._mem_unionpay[on] = response
                self._stats = self._stats.add(disk=1)
                return response
            except (OSError, ValueError):
                pass

        url = UNIONPAY_URL.format(date=_yyyymmdd(on))
        try:
            resp = self._http.get(url)
        except httpx.HTTPError:
            return None
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            return None

        try:
            payload = resp.json()
            response = UnionPayResponse.model_validate(payload)
        except (ValueError, json.JSONDecodeError):
            return None

        self._atomic_write(path, resp.content)
        self._mem_unionpay[on] = response
        self._stats = self._stats.add(network=1)
        return response

    # ---- Frankfurter --------------------------------------------------------

    def _try_frankfurter(self, on: date, src: CurrencyCode, dst: CurrencyCode) -> Decimal | None:
        path = self._cache_dir / "frankfurter" / f"{_yyyymmdd(on)}_{src}_{dst}.json"
        if not self._no_cache and self._is_fresh(path, on):
            try:
                payload = json.loads(path.read_bytes())
                rate = FrankfurterRate.model_validate(payload)
                self._stats = self._stats.add(disk=1)
                return rate.rate
            except (OSError, ValueError):
                pass

        url = FRANKFURTER_URL.format(src=src, dst=dst)
        params = {"date": on.isoformat()}
        try:
            resp = self._http.get(url, params=params)
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400:
            return None

        try:
            payload = resp.json()
            rate = FrankfurterRate.model_validate(payload)
        except (ValueError, json.JSONDecodeError):
            return None

        self._atomic_write(path, resp.content)
        self._stats = self._stats.add(network=1)
        return rate.rate

    # ---- Helpers ------------------------------------------------------------

    def _is_fresh(self, path: Path, on: date) -> bool:
        if not path.exists():
            return False
        # Use UTC consistently so the past/today boundary doesn't drift across
        # local-clock midnight rollover.
        if on < _today_utc():
            return True
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        return (datetime.now(UTC) - mtime) < TODAY_TTL

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        # Unique temp filename so concurrent writers don't clobber each other's
        # in-flight temp files (which would otherwise race on os.replace).
        fd, tmp_str = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=path.stem + ".",
            suffix=".tmp",
        )
        tmp = Path(tmp_str)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def _yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _today_utc() -> date:
    return datetime.now(UTC).date()
