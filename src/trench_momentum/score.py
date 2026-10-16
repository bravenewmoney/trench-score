"""Pure momentum score.

Do not change this formula unless explicitly asked.

Each term is clamped to [0, 1] and then weighted:

- volume acceleration, weight 30:
  volume_5m / (volume_1h / 12)
- buy pressure, weight 25:
  buys_5m / (buys_5m + sells_5m)
- transaction acceleration, weight 20:
  txns_5m / (txns_1h / 12)
- five-minute price change, weight 15:
  price_change_5m / 50
  Price change is a percent. +50% saturates the term. Negative prints score 0.
- penalty, weight 20, not stacked:
  1 when volume_5m / liquidity_usd > 20 or liquidity_usd < 10000

The positive weights sum to 90. The total is clamped to [0, 100].

Volume acceleration is 0 when volume_1h is 0. Transaction acceleration is 0
when txns_1h is 0. An empty baseline does not saturate the term. If there
are no 5m buys and no 5m sells, buy pressure is 0.

``curve_score`` is a separate function for pumpfun rows with null liquidity.
It does not change the weights above and it does not apply the liquidity penalty.
"""

import math

from trench_momentum.models import LIQUIDITY_FLOOR_USD, CurveScore, Score, Snapshot

VOLUME_WEIGHT = 30.0
BUY_WEIGHT = 25.0
TX_WEIGHT = 20.0
PRICE_WEIGHT = 15.0
PENALTY_WEIGHT = 20.0
PRICE_CHANGE_CAP_PCT = 50.0
WASH_VOLUME_TO_LIQUIDITY = 20.0

CURVE_VOLUME_WEIGHT = 40.0
CURVE_BUY_WEIGHT = 40.0
CURVE_ABSOLUTE_VOLUME_WEIGHT = 20.0
CURVE_ABSOLUTE_VOLUME_CAP_USD = 20_000.0
CURVE_MIN_TRADES = 20


def momentum_score(snapshot: Snapshot) -> Score:
    """Score one snapshot. No I/O."""

    volume_term = _clamp01(_pace_ratio(snapshot.volume_5m, snapshot.volume_1h))
    buy_term = _clamp01(_buy_pressure(snapshot.buys_5m, snapshot.sells_5m))
    tx_term = _clamp01(_pace_ratio(float(snapshot.txns_5m), float(snapshot.txns_1h)))
    price_term = _clamp01(snapshot.price_change_5m / PRICE_CHANGE_CAP_PCT)
    penalty_term = _penalty_term(snapshot.volume_5m, snapshot.liquidity_usd)
    total = (
        VOLUME_WEIGHT * volume_term
        + BUY_WEIGHT * buy_term
        + TX_WEIGHT * tx_term
        + PRICE_WEIGHT * price_term
        - PENALTY_WEIGHT * penalty_term
    )
    return Score(
        total=_clamp(total, 0.0, 100.0),
        volume_acceleration=volume_term,
        buy_pressure=buy_term,
        transaction_acceleration=tx_term,
        price_change=price_term,
        penalty=PENALTY_WEIGHT * penalty_term,
    )


def curve_score(
    *,
    volume_5m: float,
    volume_1h: float,
    buys_5m: int,
    sells_5m: int,
) -> CurveScore:
    """Score one bonding-curve tape. No I/O and no liquidity penalty.

    Fewer than 20 buys plus sells in five minutes scores 0. Volume
    acceleration is 0 when ``volume_1h`` is 0. Absolute 5-minute volume
    saturates at $20k.
    """

    buys = max(0, buys_5m)
    sells = max(0, sells_5m)
    if buys + sells < CURVE_MIN_TRADES:
        return CurveScore(
            total=0.0,
            volume_acceleration=0.0,
            buy_pressure=0.0,
            absolute_volume=0.0,
        )
    volume_term = _clamp01(_pace_ratio(volume_5m, volume_1h))
    buy_term = _clamp01(_buy_pressure(buys, sells))
    absolute_term = _clamp01(max(0.0, volume_5m) / CURVE_ABSOLUTE_VOLUME_CAP_USD)
    total = (
        CURVE_VOLUME_WEIGHT * volume_term
        + CURVE_BUY_WEIGHT * buy_term
        + CURVE_ABSOLUTE_VOLUME_WEIGHT * absolute_term
    )
    return CurveScore(
        total=_clamp(total, 0.0, 100.0),
        volume_acceleration=volume_term,
        buy_pressure=buy_term,
        absolute_volume=absolute_term,
    )


def _pace_ratio(current: float, trailing_hour: float) -> float:
    current = max(0.0, current)
    trailing_hour = max(0.0, trailing_hour)
    if trailing_hour == 0:
        return 0.0
    return current / (trailing_hour / 12.0)


def _buy_pressure(buys: int, sells: int) -> float:
    buys = max(0, buys)
    sells = max(0, sells)
    total = buys + sells
    if total == 0:
        return 0.0
    return buys / total


def _penalty_term(volume_5m: float, liquidity_usd: float) -> float:
    volume_5m = max(0.0, volume_5m)
    liquidity_usd = max(0.0, liquidity_usd)
    thin = liquidity_usd < LIQUIDITY_FLOOR_USD
    wash = liquidity_usd > 0 and (volume_5m / liquidity_usd) > WASH_VOLUME_TO_LIQUIDITY
    return 1.0 if thin or wash else 0.0


def _clamp01(value: float) -> float:
    return _clamp(value, 0.0, 1.0)


def _clamp(value: float, lower: float, upper: float) -> float:
    if math.isnan(value):
        return lower
    return min(upper, max(lower, value))
