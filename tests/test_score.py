"""Score cases. These tests do not open sockets."""

from datetime import datetime, timedelta, timezone

import pytest

from trench_momentum.models import LIQUIDITY_FLOOR_USD, Snapshot
from trench_momentum.score import (
    CURVE_ABSOLUTE_VOLUME_CAP_USD,
    CURVE_MIN_TRADES,
    WASH_VOLUME_TO_LIQUIDITY,
    curve_score,
    momentum_score,
)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def snapshot(**overrides) -> Snapshot:
    base = dict(
        pair_address="pair",
        chain="solana",
        symbol="TEST",
        liquidity_usd=50_000.0,
        market_cap=500_000.0,
        volume_5m=0.0,
        volume_1h=0.0,
        buys_5m=0,
        sells_5m=0,
        txns_5m=0,
        txns_1h=0,
        price_change_5m=0.0,
        pair_created_at=NOW - timedelta(hours=2),
    )
    base.update(overrides)
    return Snapshot(**base)


def strong(**overrides) -> Snapshot:
    """Every positive term saturated, liquidity healthy, wash ratio well under 20."""

    base = dict(
        symbol="STRONG",
        liquidity_usd=50_000.0,
        volume_5m=10_000.0,
        volume_1h=10_000.0,
        buys_5m=100,
        sells_5m=0,
        txns_5m=100,
        txns_1h=100,
        price_change_5m=80.0,
    )
    base.update(overrides)
    return snapshot(**base)


def test_strong_momentum():
    score = momentum_score(strong())
    again = momentum_score(strong())

    assert score == again
    assert score.volume_acceleration == 1
    assert score.buy_pressure == 1
    assert score.transaction_acceleration == 1
    assert score.price_change == 1
    assert score.penalty == 0
    assert score.total == pytest.approx(90)


def test_wash_volume():
    # Liquidity stays at the floor so only the volume/liquidity ratio trips the penalty.
    liquidity = 10_000.0
    volume_5m = 240_000.0
    assert liquidity >= LIQUIDITY_FLOOR_USD
    assert volume_5m / liquidity > WASH_VOLUME_TO_LIQUIDITY

    clean = momentum_score(strong())
    washed = momentum_score(
        strong(symbol="WASH", liquidity_usd=liquidity, volume_5m=volume_5m, volume_1h=volume_5m)
    )

    assert washed.volume_acceleration == 1
    assert washed.penalty == 20
    assert washed.total == pytest.approx(clean.total - 20)
    assert washed.total == pytest.approx(70)


def test_thin_liquidity():
    liquidity = 5_000.0
    volume_5m = 10_000.0
    assert liquidity < LIQUIDITY_FLOOR_USD
    assert volume_5m / liquidity <= WASH_VOLUME_TO_LIQUIDITY

    clean = momentum_score(strong())
    thin = momentum_score(strong(symbol="DUST", liquidity_usd=liquidity, volume_5m=volume_5m))

    assert thin.penalty == 20
    assert thin.total == pytest.approx(clean.total - 20)

    stacked = momentum_score(
        strong(symbol="BOTH", liquidity_usd=1_000.0, volume_5m=100_000.0, volume_1h=100_000.0)
    )
    assert stacked.penalty == 20
    assert stacked.total == pytest.approx(70)


def test_flat_market():
    score = momentum_score(
        snapshot(
            symbol="FLAT",
            liquidity_usd=50_000.0,
            volume_5m=100.0,
            volume_1h=120_000.0,
            buys_5m=10,
            sells_5m=10,
            txns_5m=20,
            txns_1h=2_400,
            price_change_5m=0.0,
        )
    )

    assert score.penalty == 0
    assert score.price_change == 0
    assert score.buy_pressure == pytest.approx(0.5)
    assert score.volume_acceleration == pytest.approx(0.01)
    assert score.transaction_acceleration == pytest.approx(0.1)
    assert score.total == pytest.approx(14.8)
    assert score.total < 60


def test_terms_clamp_before_weighting():
    hot = momentum_score(
        strong(volume_5m=1_000_000, volume_1h=1_000, price_change_5m=400, txns_5m=9_000, txns_1h=10)
    )
    capped = momentum_score(strong(price_change_5m=50))
    half_move = momentum_score(strong(price_change_5m=25, volume_5m=0, volume_1h=0, txns_5m=0, txns_1h=0, buys_5m=0, sells_5m=0))
    quiet = momentum_score(snapshot(price_change_5m=-12))

    assert hot.volume_acceleration == 1
    assert hot.transaction_acceleration == 1
    assert hot.price_change == 1
    assert hot.total == pytest.approx(90)
    assert capped.price_change == 1
    assert half_move.price_change == pytest.approx(0.5)
    assert half_move.total == pytest.approx(7.5)
    assert quiet.price_change == 0
    assert quiet.total == 0


def test_penalty_boundaries_and_empty_books():
    at_ratio = momentum_score(strong(liquidity_usd=10_000, volume_5m=200_000, volume_1h=200_000))
    at_floor = momentum_score(strong(liquidity_usd=10_000))
    empty = momentum_score(snapshot())
    burst_on_empty_hour = momentum_score(snapshot(volume_5m=100, txns_5m=4, buys_5m=4))

    assert 200_000 / 10_000 == WASH_VOLUME_TO_LIQUIDITY
    assert at_ratio.penalty == 0
    assert at_floor.penalty == 0
    assert empty.total == 0
    assert empty.buy_pressure == 0
    assert burst_on_empty_hour.volume_acceleration == 0
    assert burst_on_empty_hour.transaction_acceleration == 0
    volume_only = momentum_score(snapshot(volume_5m=100, volume_1h=0, txns_5m=10, txns_1h=10))
    tx_only = momentum_score(snapshot(volume_5m=100, volume_1h=100, txns_5m=10, txns_1h=0))
    assert volume_only.volume_acceleration == 0
    assert volume_only.transaction_acceleration == 1
    assert tx_only.volume_acceleration == 1
    assert tx_only.transaction_acceleration == 0


def test_curve_score_is_separate_from_the_amm_penalty():
    hot = curve_score(volume_5m=100_000, volume_1h=100_000, buys_5m=100, sells_5m=0)
    paced = curve_score(volume_5m=5_000, volume_1h=9_000, buys_5m=40, sells_5m=10)
    thin_tape = curve_score(volume_5m=100_000, volume_1h=100_000, buys_5m=CURVE_MIN_TRADES - 1, sells_5m=0)
    exact = curve_score(volume_5m=0, volume_1h=0, buys_5m=CURVE_MIN_TRADES, sells_5m=0)
    empty_hour = curve_score(volume_5m=CURVE_ABSOLUTE_VOLUME_CAP_USD, volume_1h=0, buys_5m=40, sells_5m=10)
    capped = curve_score(volume_5m=CURVE_ABSOLUTE_VOLUME_CAP_USD, volume_1h=CURVE_ABSOLUTE_VOLUME_CAP_USD, buys_5m=0, sells_5m=20)
    half_volume = curve_score(volume_5m=10_000, volume_1h=10_000, buys_5m=0, sells_5m=20)

    assert hot.volume_acceleration == 1
    assert hot.buy_pressure == 1
    assert hot.absolute_volume == 1
    assert hot.total == pytest.approx(100)
    assert paced.volume_acceleration == 1
    assert paced.buy_pressure == pytest.approx(0.8)
    assert paced.absolute_volume == pytest.approx(0.25)
    assert paced.total == pytest.approx(77)
    assert thin_tape.total == 0
    assert thin_tape.volume_acceleration == 0
    assert thin_tape.buy_pressure == 0
    assert thin_tape.absolute_volume == 0
    assert exact.volume_acceleration == 0
    assert exact.buy_pressure == 1
    assert exact.absolute_volume == 0
    assert exact.total == pytest.approx(40)
    assert empty_hour.volume_acceleration == 0
    assert empty_hour.buy_pressure == pytest.approx(0.8)
    assert empty_hour.absolute_volume == 1
    assert empty_hour.total == pytest.approx(52)
    assert capped.absolute_volume == 1
    assert half_volume.absolute_volume == pytest.approx(0.5)
