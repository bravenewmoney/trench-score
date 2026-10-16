"""Shared records for scoring, filtering, and alerts."""

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field

V1_CHAIN = "solana"
LIQUIDITY_FLOOR_USD = 10_000.0
MAX_MARKET_CAP_USD = 3_000_000.0
MAX_PAIR_AGE = timedelta(hours=48)
DEFAULT_ALERT_THRESHOLD = 60.0


class Snapshot(BaseModel):
    """One pair observation. The score function reads only the numeric fields."""

    model_config = ConfigDict(extra="forbid")

    pair_address: str
    chain: str
    symbol: str
    name: str = ""
    dex: str = ""
    price_usd: float | None = None
    liquidity_usd: float = Field(ge=0)
    market_cap: float | None = Field(default=None, ge=0)
    fdv: float | None = Field(default=None, ge=0)
    volume_5m: float = Field(ge=0)
    volume_1h: float = Field(ge=0)
    buys_5m: int = Field(ge=0)
    sells_5m: int = Field(ge=0)
    txns_5m: int = Field(ge=0)
    txns_1h: int = Field(ge=0)
    price_change_5m: float
    pair_created_at: datetime | None = None


class Score(BaseModel):
    """Weighted momentum score.

    The four ratio fields are the clamped [0, 1] terms. ``penalty`` and
    ``total`` are points. ``total`` is clamped to [0, 100].
    """

    model_config = ConfigDict(frozen=True)

    total: float = Field(ge=0, le=100)
    volume_acceleration: float = Field(ge=0, le=1)
    buy_pressure: float = Field(ge=0, le=1)
    transaction_acceleration: float = Field(ge=0, le=1)
    price_change: float = Field(ge=0, le=1)
    penalty: float = Field(ge=0, le=20)


class CurveScore(BaseModel):
    """Pumpfun bonding-curve score. No liquidity penalty.

    The three ratio fields are the clamped [0, 1] terms. ``total`` is points
    in [0, 100]. A 5-minute tape under 20 trades is all zeros.
    """

    model_config = ConfigDict(frozen=True)

    total: float = Field(ge=0, le=100)
    volume_acceleration: float = Field(ge=0, le=1)
    buy_pressure: float = Field(ge=0, le=1)
    absolute_volume: float = Field(ge=0, le=1)


class Alert(BaseModel):
    """One threshold crossing, written as a single alerts.jsonl line."""

    recorded_at: datetime
    pair_address: str
    chain: str
    symbol: str
    score: float
    threshold: float
    liquidity_usd: float
    market_cap: float | None
    price_change_5m: float
    lane: str = "amm"


def v1_rejection(snapshot: Snapshot, now: datetime) -> str | None:
    """Return the first v1 filter the pair fails, or None if it passes.

    Missing market cap or pair age fails closed. Liquidity at the floor,
    market cap at the cap, and age at 48h all pass.
    """

    if snapshot.chain.lower() != V1_CHAIN:
        return "chain"
    if snapshot.liquidity_usd < LIQUIDITY_FLOOR_USD:
        return "liquidity"
    if snapshot.market_cap is None or snapshot.market_cap > MAX_MARKET_CAP_USD:
        return "market cap"
    if snapshot.pair_created_at is None:
        return "age"
    age = _as_utc(now) - _as_utc(snapshot.pair_created_at)
    if age > MAX_PAIR_AGE:
        return "age"
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
