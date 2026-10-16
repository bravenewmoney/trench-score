"""Offline CLI, parser, filters, and alert latch. No network."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from trench_momentum.main import POLL_INTERVAL_SECONDS, main
from trench_momentum.models import MAX_MARKET_CAP_USD, MAX_PAIR_AGE, Snapshot, v1_rejection
from trench_momentum.score import curve_score, momentum_score
from trench_momentum.sources.dexscreener import PairDiagnostic, PollBatch, parse_pair
from trench_momentum.store import Store

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _pair(**overrides) -> Snapshot:
    base = dict(
        pair_address="pair",
        chain="solana",
        symbol="TEST",
        liquidity_usd=50_000.0,
        market_cap=500_000.0,
        volume_5m=10_000.0,
        volume_1h=10_000.0,
        buys_5m=100,
        sells_5m=0,
        txns_5m=100,
        txns_1h=100,
        price_change_5m=80.0,
        pair_created_at=NOW - timedelta(hours=2),
    )
    base.update(overrides)
    return Snapshot(**base)


def test_parse_pair_is_local():
    parsed = parse_pair(
        {
            "chainId": "Solana",
            "dexId": "raydium",
            "pairAddress": "fixture-momo",
            "baseToken": {"symbol": "MOMO", "name": "Momo"},
            "priceUsd": "0.0042",
            "txns": {"m5": {"buys": 80, "sells": 20}, "h1": {"buys": 50, "sells": 50}},
            "volume": {"m5": 60000, "h1": 60000},
            "priceChange": {"m5": 40},
            "liquidity": {"usd": 80000},
            "marketCap": 750000,
            "pairCreatedAt": 1_759_401_600_000,
        }
    )
    assert parsed.chain == "solana"
    assert parsed.symbol == "MOMO"
    assert parsed.price_usd == pytest.approx(0.0042)
    assert parsed.txns_5m == 100
    assert parsed.txns_1h == 100
    assert parsed.pair_created_at == datetime.fromtimestamp(1_759_401_600, tz=timezone.utc)
    assert 0 <= momentum_score(parsed).total <= 100


def test_v1_filter_boundaries():
    assert v1_rejection(_pair(liquidity_usd=10_000), NOW) is None
    assert v1_rejection(_pair(liquidity_usd=9_999), NOW) == "liquidity"
    assert v1_rejection(_pair(market_cap=MAX_MARKET_CAP_USD), NOW) is None
    assert v1_rejection(_pair(market_cap=MAX_MARKET_CAP_USD + 1), NOW) == "market cap"
    assert v1_rejection(_pair(market_cap=None), NOW) == "market cap"
    assert v1_rejection(_pair(pair_created_at=NOW - MAX_PAIR_AGE), NOW) is None
    assert v1_rejection(_pair(pair_created_at=NOW - MAX_PAIR_AGE - timedelta(seconds=1)), NOW) == "age"
    assert v1_rejection(_pair(pair_created_at=None), NOW) == "age"
    assert v1_rejection(_pair(chain="ethereum"), NOW) == "chain"


def test_alert_only_on_threshold_cross(tmp_path):
    db_path = tmp_path / "snapshots.sqlite"
    alerts_path = tmp_path / "alerts.jsonl"
    high = _pair(symbol="MOMO", pair_address="momo")
    low = _pair(
        symbol="MOMO",
        pair_address="momo",
        volume_5m=0,
        volume_1h=0,
        buys_5m=0,
        sells_5m=0,
        txns_5m=0,
        txns_1h=0,
        price_change_5m=0,
    )
    high_score = momentum_score(high)
    low_score = momentum_score(low)
    assert high_score.total >= 60
    assert low_score.total < 60

    with Store(db_path, alerts_path) as store:
        assert store.record(high, high_score, passes_filter=False, now=NOW) is False
        assert store.record(high, high_score, passes_filter=True, now=NOW) is True
        assert store.record(high, high_score, passes_filter=True, now=NOW) is False
        assert store.record(low, low_score, passes_filter=True, now=NOW) is False
        assert store.record(high, high_score, passes_filter=True, now=NOW) is True

    lines = alerts_path.read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(line) for line in lines]
    assert [item["symbol"] for item in parsed] == ["MOMO", "MOMO"]
    assert [item["lane"] for item in parsed] == ["amm", "amm"]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("select count(*) from snapshots").fetchone()[0] == 5


def test_offline_demo_sorts_and_does_not_realert(tmp_path, monkeypatch, capsys):
    def explode(*args, **kwargs):
        raise AssertionError("DexScreener client must not be constructed offline")

    monkeypatch.setattr("trench_momentum.main.DexScreenerClient", explode)
    db_path = tmp_path / "snapshots.sqlite"
    alerts_path = tmp_path / "alerts.jsonl"
    argv = ["--db", str(db_path), "--alerts", str(alerts_path)]

    assert main(argv) == 0
    first = alerts_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["symbol"] for line in first] == ["MOMO", "WASH"]

    assert main(argv) == 0
    assert alerts_path.read_text(encoding="utf-8").splitlines() == first

    text = capsys.readouterr().out
    for earlier, later in (
        ("GIANT", "MOMO"),
        ("MOMO", "STALE"),
        ("STALE", "WASH"),
        ("WASH", "DUST"),
        ("DUST", "FLAT"),
    ):
        assert text.index(earlier) < text.index(later)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("select count(*) from snapshots").fetchone()[0] == 12


class _DummyClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def test_live_once_does_not_sleep_or_use_the_network(tmp_path, monkeypatch, capsys):
    constructed = []

    def client_factory(*args, **kwargs):
        constructed.append(1)
        return _DummyClient()

    monkeypatch.setattr("trench_momentum.main.DexScreenerClient", client_factory)
    monkeypatch.setattr(
        "trench_momentum.main.collect_solana_snapshots",
        lambda client: PollBatch(snapshots=[_pair(symbol="MOMO", pair_address="momo")]),
    )

    def fail_sleep(seconds):
        raise AssertionError("live --once must not sleep")

    monkeypatch.setattr("trench_momentum.main.time.sleep", fail_sleep)
    db_path = tmp_path / "snapshots.sqlite"
    alerts_path = tmp_path / "alerts.jsonl"
    argv = ["live", "--once", "--db", str(db_path), "--alerts", str(alerts_path)]

    assert main(argv) == 0
    assert main(argv) == 0
    assert constructed == [1, 1]
    lines = alerts_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["symbol"] for line in lines] == ["MOMO"]
    assert "MOMO" in capsys.readouterr().out


def test_live_loop_waits_20s_until_ctrl_c(tmp_path, monkeypatch):
    sleeps = []

    monkeypatch.setattr("trench_momentum.main.DexScreenerClient", lambda *args, **kwargs: _DummyClient())
    monkeypatch.setattr(
        "trench_momentum.main.collect_solana_snapshots",
        lambda client: PollBatch(snapshots=[]),
    )

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("trench_momentum.main.time.sleep", sleep)
    code = main(
        [
            "live",
            "--db",
            str(tmp_path / "snapshots.sqlite"),
            "--alerts",
            str(tmp_path / "alerts.jsonl"),
        ]
    )

    assert POLL_INTERVAL_SECONDS == 20
    assert code == 0
    assert sleeps == [20, 20]


def test_young_pair_is_printed_and_not_alerted(tmp_path, monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    young = _pair(
        symbol="YOUNG",
        pair_address="young",
        pair_created_at=now - timedelta(minutes=10),
    )
    older = _pair(
        symbol="OLDER",
        pair_address="older",
        pair_created_at=now - timedelta(minutes=31),
    )
    monkeypatch.setattr("trench_momentum.main.DexScreenerClient", lambda *args, **kwargs: _DummyClient())
    monkeypatch.setattr(
        "trench_momentum.main.collect_solana_snapshots",
        lambda client: PollBatch(snapshots=[young, older]),
    )
    alerts_path = tmp_path / "alerts.jsonl"
    assert main(["live", "--once", "--db", str(tmp_path / "snapshots.sqlite"), "--alerts", str(alerts_path)]) == 0
    assert [json.loads(line)["symbol"] for line in alerts_path.read_text(encoding="utf-8").splitlines()] == ["OLDER"]
    text = capsys.readouterr().out
    assert "YOUNG" in text and "OLDER" in text
    young_line = next(line for line in text.splitlines() if "YOUNG" in line)
    older_line = next(line for line in text.splitlines() if "OLDER" in line)
    assert "age" in young_line
    assert "cross" in older_line


def _curve_row(*, symbol: str, address: str, created_at: datetime | None) -> PairDiagnostic:
    return PairDiagnostic(
        symbol=symbol,
        dex_id="pumpfun",
        pair_address=address,
        liquidity=None,
        volume_m5=20_000,
        volume_h1=20_000,
        buys_5m=40,
        sells_5m=10,
        chain="solana",
        pair_created_at=created_at,
        market_cap=180_000,
        price_change_5m=12.5,
    )


def test_curve_alert_uses_its_own_lane_and_age_gate(tmp_path, monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    assert curve_score(volume_5m=20_000, volume_1h=20_000, buys_5m=40, sells_5m=10).total >= 60
    young = _curve_row(symbol="CYOUNG", address="curve-young", created_at=now - timedelta(minutes=10))
    older = _curve_row(symbol="COLDER", address="curve-older", created_at=now - timedelta(minutes=31))
    undated = _curve_row(symbol="CUNDATED", address="curve-undated", created_at=None)
    monkeypatch.setattr("trench_momentum.main.DexScreenerClient", lambda *args, **kwargs: _DummyClient())
    monkeypatch.setattr(
        "trench_momentum.main.collect_solana_snapshots",
        lambda client: PollBatch(snapshots=[], curve=(young, older, undated)),
    )
    alerts_path = tmp_path / "alerts.jsonl"
    argv = ["live", "--once", "--db", str(tmp_path / "snapshots.sqlite"), "--alerts", str(alerts_path)]

    assert main(argv) == 0
    first = [json.loads(line) for line in alerts_path.read_text(encoding="utf-8").splitlines()]
    assert [(item["symbol"], item["lane"]) for item in first] == [("COLDER", "curve")]

    assert main(argv) == 0
    assert [json.loads(line) for line in alerts_path.read_text(encoding="utf-8").splitlines()] == first

    text = capsys.readouterr().out
    young_line = next(line for line in text.splitlines() if "CYOUNG" in line)
    older_line = next(line for line in text.splitlines() if "COLDER" in line)
    undated_line = next(line for line in text.splitlines() if "CUNDATED" in line)
    assert "age" in young_line
    assert "cross" in older_line
    assert "age" not in undated_line
    assert "cross" not in undated_line


def test_curve_latch_is_independent_of_amm_snapshots(tmp_path):
    db_path = tmp_path / "snapshots.sqlite"
    alerts_path = tmp_path / "alerts.jsonl"
    hot = curve_score(volume_5m=20_000, volume_1h=20_000, buys_5m=40, sells_5m=10)
    quiet = curve_score(volume_5m=0, volume_1h=0, buys_5m=10, sells_5m=10)
    assert hot.total >= 60
    assert quiet.total < 60
    fields = dict(
        pair_address="curve-pair",
        chain="solana",
        symbol="CURVE",
        volume_5m=20_000,
        volume_1h=20_000,
        buys_5m=40,
        sells_5m=10,
        market_cap=None,
        price_change_5m=0.0,
        now=NOW,
    )
    quiet_fields = dict(fields, score=quiet, volume_5m=0, volume_1h=0, buys_5m=10, sells_5m=10)
    with Store(db_path, alerts_path) as store:
        assert store.record_curve(**fields, score=hot) is True
        assert store.record_curve(**fields, score=hot) is False
        assert store.record_curve(**quiet_fields) is False
        assert store.record_curve(**fields, score=hot) is True
    lines = [json.loads(line) for line in alerts_path.read_text(encoding="utf-8").splitlines()]
    assert [item["lane"] for item in lines] == ["curve", "curve"]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("select count(*) from snapshots").fetchone()[0] == 0
        assert conn.execute("select count(*) from curve_observations").fetchone()[0] == 4


def test_once_without_live_is_rejected():
    with pytest.raises(SystemExit):
        main(["--once"])
