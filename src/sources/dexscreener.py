"""DexScreener client and typed parsers.

The parsers are pure. The client is the only piece that opens a socket, and
``main`` does not construct it unless the ``live`` command is used.

Discovery is ``GET /token-profiles/latest/v1`` (60 requests/minute). That feed
is the latest token profile pages, not a list of newly created pairs.
Enrichment is ``GET /token-pairs/v1/solana/{tokenAddress}`` (300 requests/minute).
The website is never scraped.
"""

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from trench_momentum.models import Snapshot

BASE_URL = "https://api.dexscreener.com"
PROFILES_PATH = "/token-profiles/latest/v1"
TOKEN_PAIRS_PATH = "/token-pairs/v1/{chain}/{token_address}"
PROFILE_REQUESTS_PER_MINUTE = 60
PAIR_REQUESTS_PER_MINUTE = 300


@dataclass(frozen=True)
class TokenProfile:
    chain_id: str
    token_address: str
    url: str = ""


@dataclass(frozen=True)
class PairDiagnostic:
    """Raw DexScreener fields for one pair, scored or skipped for empty liquidity."""

    symbol: str
    dex_id: str
    pair_address: str
    liquidity: Any
    volume_m5: Any
    volume_h1: Any
    buys_5m: int | None = None
    sells_5m: int | None = None
    chain: str = "solana"
    pair_created_at: datetime | None = None
    market_cap: float | None = None
    price_change_5m: float = 0.0


@dataclass(frozen=True)
class PollBatch:
    snapshots: list[Snapshot]
    skipped: int = 0
    diagnostics: tuple[PairDiagnostic, ...] = ()
    zero_liquidity: tuple[PairDiagnostic, ...] = ()
    curve: tuple[PairDiagnostic, ...] = ()


class _RollingLimiter:
    """Block only when the last minute already holds ``per_minute`` calls."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._stamps: list[float] = []

    def acquire(self) -> None:
        if self.per_minute <= 0:
            return
        now = time.monotonic()
        self._stamps = [stamp for stamp in self._stamps if now - stamp < 60]
        if len(self._stamps) >= self.per_minute:
            delay = 60 - (now - self._stamps[0])
            if delay > 0:
                time.sleep(delay)
            now = time.monotonic()
            self._stamps = [stamp for stamp in self._stamps if now - stamp < 60]
        self._stamps.append(time.monotonic())


class DexScreenerClient:
    """Read-only client for the two public DexScreener endpoints v1 uses."""

    def __init__(
        self,
        http: httpx.Client | None = None,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
        profile_limit: int = PROFILE_REQUESTS_PER_MINUTE,
        pair_limit: int = PAIR_REQUESTS_PER_MINUTE,
    ) -> None:
        self._owns_client = http is None
        self._http = http or httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"Accept": "application/json", "User-Agent": "trench-momentum/0.1"},
        )
        self._base_url = base_url.rstrip("/")
        self._profile_limit = _RollingLimiter(profile_limit)
        self._pair_limit = _RollingLimiter(pair_limit)

    def __enter__(self) -> "DexScreenerClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def latest_token_profiles(self, *, chain: str = "solana") -> list[TokenProfile]:
        """Latest token profiles for one chain. Performs one network request."""

        self._profile_limit.acquire()
        response = self._http.get(f"{self._base_url}{PROFILES_PATH}")
        response.raise_for_status()
        return parse_token_profiles(response.json(), chain=chain)

    def token_pairs(self, chain: str, token_address: str) -> list[Mapping[str, Any]]:
        """Pairs for one token. Performs one network request. 404 is an empty list."""

        self._pair_limit.acquire()
        path = TOKEN_PAIRS_PATH.format(
            chain=quote(chain, safe=""),
            token_address=quote(token_address, safe=""),
        )
        response = self._http.get(f"{self._base_url}{path}")
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return pair_objects(response.json())


def collect_solana_snapshots(client: DexScreenerClient) -> PollBatch:
    """Discover Solana profiles and keep each token's deepest funded pair.

    Pairs whose ``liquidity.usd`` is missing or 0 are not scored and are not
    filter rejects. Among the pairs that remain, the highest ``liquidity.usd``
    wins. Base-token pools are preferred over a deeper pool where this token
    is only the quote.
    """

    profiles = client.latest_token_profiles(chain="solana")
    snapshots: list[Snapshot] = []
    diagnostics: list[PairDiagnostic] = []
    zero_liquidity: list[PairDiagnostic] = []
    curve: list[PairDiagnostic] = []
    seen: set[str] = set()
    curve_seen: set[str] = set()
    skipped = 0
    for profile in profiles:
        try:
            raw_pairs = client.token_pairs(profile.chain_id, profile.token_address)
        except httpx.HTTPError:
            skipped += 1
            continue
        token_curve: list[PairDiagnostic] = []
        for raw in raw_pairs:
            if not _is_pumpfun_curve(raw):
                continue
            view = _pair_diagnostic(raw)
            if not view.pair_address or view.pair_address in curve_seen:
                continue
            token_curve.append(view)
        chosen = select_deepest_pair(raw_pairs, token_address=profile.token_address)
        # A funded AMM pair for this token replaces the bonding-curve row.
        if chosen is not None:
            token_curve = []
        for view in token_curve:
            curve_seen.add(view.pair_address)
            curve.append(view)
        if chosen is None:
            sample = _zero_liquidity_sample(raw_pairs, token_address=profile.token_address)
            if sample is not None and not _is_pumpfun_curve(sample):
                zero_liquidity.append(_pair_diagnostic(sample))
            continue
        try:
            snapshot = parse_pair(chosen)
        except (ValueError, TypeError, ValidationError):
            skipped += 1
            continue
        if snapshot.pair_address in seen:
            continue
        seen.add(snapshot.pair_address)
        snapshots.append(snapshot)
        diagnostics.append(_pair_diagnostic(chosen))
    return PollBatch(
        snapshots=snapshots,
        skipped=skipped,
        diagnostics=tuple(diagnostics),
        zero_liquidity=tuple(zero_liquidity),
        curve=tuple(curve),
    )


def parse_token_profiles(payload: object, *, chain: str = "solana") -> list[TokenProfile]:
    """Keep profiles whose ``chainId`` matches, in feed order, one row per address."""

    wanted = chain.strip().lower()
    profiles: list[TokenProfile] = []
    seen: set[str] = set()
    for item in _object_list(payload):
        chain_id = str(item.get("chainId") or "").strip().lower()
        token_address = str(item.get("tokenAddress") or "").strip()
        if chain_id != wanted or not token_address or token_address in seen:
            continue
        seen.add(token_address)
        profiles.append(
            TokenProfile(
                chain_id=chain_id,
                token_address=token_address,
                url=str(item.get("url") or "").strip(),
            )
        )
    return profiles


def pair_objects(payload: object) -> list[Mapping[str, Any]]:
    """Accept a bare pair list or an object with a ``pairs`` list."""

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        raw = payload.get("pairs")
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, Mapping)]
    raise ValueError("DexScreener pair payload must be a list")


def select_deepest_pair(
    pairs: object,
    *,
    token_address: str | None = None,
) -> Mapping[str, Any] | None:
    """Pick the highest positive ``liquidity.usd``.

    Pairs with missing or zero ``liquidity.usd`` are ignored. Pools whose base
    token is ``token_address`` are preferred. A deeper pool where this token
    is only the quote is used when no funded base pool is present. Ties keep
    the earlier pair. Returns None when every pair is unfunded.
    """

    try:
        items = pair_objects(pairs)
    except ValueError:
        return None
    funded = [item for item in items if _positive_liquidity_usd(item) is not None]
    if token_address:
        matched = [item for item in funded if _base_address(item) == token_address]
        if matched:
            funded = matched
    best: Mapping[str, Any] | None = None
    best_liquidity = -1.0
    for item in funded:
        liquidity = _positive_liquidity_usd(item) or 0.0
        if best is None or liquidity > best_liquidity:
            best = item
            best_liquidity = liquidity
    return best


def parse_pair(raw: Mapping[str, Any]) -> Snapshot:
    """Map one DexScreener pair object into a Snapshot."""

    if not isinstance(raw, Mapping):
        raise ValueError("pair must be an object")
    pair_address = str(raw.get("pairAddress") or "").strip()
    chain = str(raw.get("chainId") or "").strip().lower()
    if not pair_address:
        raise ValueError("pair is missing pairAddress")
    if not chain:
        raise ValueError(f"{pair_address} is missing chainId")

    base = raw.get("baseToken") if isinstance(raw.get("baseToken"), Mapping) else {}
    txns = raw.get("txns") if isinstance(raw.get("txns"), Mapping) else {}
    m5 = txns.get("m5") if isinstance(txns.get("m5"), Mapping) else {}
    h1 = txns.get("h1") if isinstance(txns.get("h1"), Mapping) else {}
    volume = raw.get("volume") if isinstance(raw.get("volume"), Mapping) else {}
    price_change = raw.get("priceChange") if isinstance(raw.get("priceChange"), Mapping) else {}
    liquidity = raw.get("liquidity") if isinstance(raw.get("liquidity"), Mapping) else {}

    buys_5m = _count(m5.get("buys"))
    sells_5m = _count(m5.get("sells"))
    symbol = str(base.get("symbol") or "").strip() or "UNKNOWN"
    market_cap = _optional_non_negative(raw.get("marketCap"))
    fdv = _optional_non_negative(raw.get("fdv"))
    if market_cap is None:
        market_cap = fdv
    return Snapshot(
        pair_address=pair_address,
        chain=chain,
        symbol=symbol,
        name=str(base.get("name") or "").strip(),
        dex=str(raw.get("dexId") or "").strip(),
        price_usd=_optional_non_negative(raw.get("priceUsd")),
        liquidity_usd=_non_negative(liquidity.get("usd")),
        market_cap=market_cap,
        fdv=fdv,
        volume_5m=_non_negative(volume.get("m5")),
        volume_1h=_non_negative(volume.get("h1")),
        buys_5m=buys_5m,
        sells_5m=sells_5m,
        txns_5m=buys_5m + sells_5m,
        txns_1h=_count(h1.get("buys")) + _count(h1.get("sells")),
        price_change_5m=_number(price_change.get("m5")),
        pair_created_at=_timestamp(raw.get("pairCreatedAt")),
    )


def _object_list(payload: object) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("profiles", "data"):
            raw = payload.get(key)
            if isinstance(raw, list):
                return [item for item in raw if isinstance(item, Mapping)]
    raise ValueError("DexScreener profile payload must be a list")


def _base_address(pair: Mapping[str, Any]) -> str:
    base = pair.get("baseToken")
    if not isinstance(base, Mapping):
        return ""
    return str(base.get("address") or "").strip()


def _payload_count(window: Mapping[str, Any], key: str) -> int | None:
    if key not in window:
        return None
    raw = window.get(key)
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _is_pumpfun_curve(raw: Mapping[str, Any]) -> bool:
    """A pumpfun pair with null liquidity is still on the bonding curve."""

    dex_id = str(raw.get("dexId") or "").strip().lower()
    return dex_id == "pumpfun" and raw.get("liquidity") is None


def _positive_liquidity_usd(pair: Mapping[str, Any]) -> float | None:
    """``liquidity.usd`` when it is a finite number above 0, else None."""

    liquidity = pair.get("liquidity")
    if not isinstance(liquidity, Mapping) or "usd" not in liquidity:
        return None
    raw = liquidity.get("usd")
    if raw is None or raw == "":
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number) or number <= 0:
        return None
    return number


def _zero_liquidity_sample(
    pairs: object,
    *,
    token_address: str | None,
) -> Mapping[str, Any] | None:
    """One unfunded pair for this token, preferring the base token."""

    try:
        items = pair_objects(pairs)
    except ValueError:
        return None
    if token_address:
        matched = [item for item in items if _base_address(item) == token_address]
        if matched:
            items = matched
    for item in items:
        if _positive_liquidity_usd(item) is None:
            return item
    return None


def _pair_diagnostic(raw: Mapping[str, Any]) -> PairDiagnostic:
    base = raw.get("baseToken") if isinstance(raw.get("baseToken"), Mapping) else {}
    volume = raw.get("volume") if isinstance(raw.get("volume"), Mapping) else {}
    txns = raw.get("txns") if isinstance(raw.get("txns"), Mapping) else {}
    m5 = txns.get("m5") if isinstance(txns.get("m5"), Mapping) else {}
    price_change = raw.get("priceChange") if isinstance(raw.get("priceChange"), Mapping) else {}
    symbol = str(base.get("symbol") or "").strip() or "UNKNOWN"
    market_cap = _optional_non_negative(raw.get("marketCap"))
    if market_cap is None:
        market_cap = _optional_non_negative(raw.get("fdv"))
    chain = str(raw.get("chainId") or "solana").strip().lower() or "solana"
    return PairDiagnostic(
        symbol=symbol,
        dex_id=str(raw.get("dexId") or "").strip(),
        pair_address=str(raw.get("pairAddress") or "").strip(),
        liquidity=raw.get("liquidity"),
        volume_m5=volume.get("m5"),
        volume_h1=volume.get("h1"),
        buys_5m=_payload_count(m5, "buys"),
        sells_5m=_payload_count(m5, "sells"),
        chain=chain,
        pair_created_at=_timestamp(raw.get("pairCreatedAt")),
        market_cap=market_cap,
        price_change_5m=_number(price_change.get("m5")),
    )


def _number(value: object) -> float:
    if value is None or value == "":
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"expected a number, got {value!r}") from exc
    if math.isnan(number) or math.isinf(number):
        return 0.0
    return number


def _non_negative(value: object) -> float:
    return max(0.0, _number(value))


def _optional_non_negative(value: object) -> float | None:
    if value is None or value == "":
        return None
    number = _number(value)
    if number < 0:
        return None
    return number


def _count(value: object) -> int:
    return int(_non_negative(value))


def _timestamp(value: object) -> datetime | None:
    """``pairCreatedAt`` is unix milliseconds. Smaller values are treated as seconds."""

    if value is None or value == "":
        return None
    seconds = _number(value)
    if seconds <= 0:
        return None
    if seconds > 10_000_000_000:
        seconds = seconds / 1000.0
    return datetime.fromtimestamp(seconds, tz=timezone.utc)
