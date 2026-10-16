"""SQLite snapshots and alerts.jsonl.

Every scored observation is inserted into sqlite. alerts.jsonl gains a line
only when a pair that passes the v1 filters crosses the threshold from below
(or is seen inside the filter for the first time already above it).

The latch follows the last in-universe snapshot. Filtered rows are stored
and do not arm or clear it. A later in-universe poll that is still at or
above the threshold does not alert again. The pair must print below the
threshold on a poll that passes the filters before the next crossing is written.

Curve rows use a separate table and a separate latch. Those alert lines set
``lane`` to ``curve``. They do not pass through the AMM filter.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from trench_momentum.models import DEFAULT_ALERT_THRESHOLD, Alert, CurveScore, Score, Snapshot


class Store:
    def __init__(self, db_path: Path, alerts_path: Path) -> None:
        self.db_path = db_path
        self.alerts_path = alerts_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def record(
        self,
        snapshot: Snapshot,
        score: Score,
        *,
        passes_filter: bool,
        threshold: float = DEFAULT_ALERT_THRESHOLD,
        now: datetime | None = None,
    ) -> bool:
        """Persist the snapshot. Return True when a new alert line is written."""

        moment = _as_utc(now or datetime.now(timezone.utc))
        with self._conn:
            previous = self._previous_score(snapshot.pair_address)
            self._insert_snapshot(snapshot, score, passes_filter=passes_filter, now=moment)
        crossed = score.total >= threshold and (previous is None or previous < threshold)
        if not (crossed and passes_filter):
            return False
        alert = Alert(
            recorded_at=moment,
            pair_address=snapshot.pair_address,
            chain=snapshot.chain,
            symbol=snapshot.symbol,
            score=score.total,
            threshold=threshold,
            liquidity_usd=snapshot.liquidity_usd,
            market_cap=snapshot.market_cap,
            price_change_5m=snapshot.price_change_5m,
            lane="amm",
        )
        self._append_alert(alert)
        return True

    def record_curve(
        self,
        *,
        pair_address: str,
        chain: str,
        symbol: str,
        score: CurveScore,
        volume_5m: float,
        volume_1h: float,
        buys_5m: int,
        sells_5m: int,
        market_cap: float | None,
        price_change_5m: float,
        threshold: float = DEFAULT_ALERT_THRESHOLD,
        now: datetime | None = None,
    ) -> bool:
        """Persist one curve observation. Alert only on a fresh cross of the threshold."""

        moment = _as_utc(now or datetime.now(timezone.utc))
        with self._conn:
            previous = self._previous_curve_score(pair_address)
            self._insert_curve(
                pair_address=pair_address,
                chain=chain,
                symbol=symbol,
                score=score,
                volume_5m=volume_5m,
                volume_1h=volume_1h,
                buys_5m=buys_5m,
                sells_5m=sells_5m,
                now=moment,
            )
        crossed = score.total >= threshold and (previous is None or previous < threshold)
        if not crossed:
            return False
        alert = Alert(
            recorded_at=moment,
            pair_address=pair_address,
            chain=chain,
            symbol=symbol,
            score=score.total,
            threshold=threshold,
            liquidity_usd=0.0,
            market_cap=market_cap,
            price_change_5m=price_change_5m,
            lane="curve",
        )
        self._append_alert(alert)
        return True

    def _init_db(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                pair_address TEXT NOT NULL,
                chain TEXT NOT NULL,
                symbol TEXT NOT NULL,
                score REAL NOT NULL,
                volume_acceleration REAL NOT NULL,
                buy_pressure REAL NOT NULL,
                transaction_acceleration REAL NOT NULL,
                price_change REAL NOT NULL,
                penalty REAL NOT NULL,
                liquidity_usd REAL NOT NULL,
                market_cap REAL,
                volume_5m REAL NOT NULL,
                volume_1h REAL NOT NULL,
                buys_5m INTEGER NOT NULL,
                sells_5m INTEGER NOT NULL,
                txns_5m INTEGER NOT NULL,
                txns_1h INTEGER NOT NULL,
                price_change_5m REAL NOT NULL,
                passes_filter INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_pair
                ON snapshots(pair_address, id DESC);
            CREATE TABLE IF NOT EXISTS curve_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                pair_address TEXT NOT NULL,
                chain TEXT NOT NULL,
                symbol TEXT NOT NULL,
                score REAL NOT NULL,
                volume_acceleration REAL NOT NULL,
                buy_pressure REAL NOT NULL,
                absolute_volume REAL NOT NULL,
                volume_5m REAL NOT NULL,
                volume_1h REAL NOT NULL,
                buys_5m INTEGER NOT NULL,
                sells_5m INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_curve_pair
                ON curve_observations(pair_address, id DESC);
            """
        )

    def _previous_score(self, pair_address: str) -> float | None:
        row = self._conn.execute(
            """
            SELECT score FROM snapshots
            WHERE pair_address = ? AND passes_filter = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (pair_address,),
        ).fetchone()
        if row is None:
            return None
        return float(row["score"])

    def _previous_curve_score(self, pair_address: str) -> float | None:
        row = self._conn.execute(
            """
            SELECT score FROM curve_observations
            WHERE pair_address = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (pair_address,),
        ).fetchone()
        if row is None:
            return None
        return float(row["score"])

    def _insert_curve(
        self,
        *,
        pair_address: str,
        chain: str,
        symbol: str,
        score: CurveScore,
        volume_5m: float,
        volume_1h: float,
        buys_5m: int,
        sells_5m: int,
        now: datetime,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO curve_observations (
                recorded_at, pair_address, chain, symbol, score,
                volume_acceleration, buy_pressure, absolute_volume,
                volume_5m, volume_1h, buys_5m, sells_5m
            ) VALUES (
                :recorded_at, :pair_address, :chain, :symbol, :score,
                :volume_acceleration, :buy_pressure, :absolute_volume,
                :volume_5m, :volume_1h, :buys_5m, :sells_5m
            )
            """,
            {
                "recorded_at": now.isoformat(timespec="seconds"),
                "pair_address": pair_address,
                "chain": chain,
                "symbol": symbol,
                "score": score.total,
                "volume_acceleration": score.volume_acceleration,
                "buy_pressure": score.buy_pressure,
                "absolute_volume": score.absolute_volume,
                "volume_5m": volume_5m,
                "volume_1h": volume_1h,
                "buys_5m": buys_5m,
                "sells_5m": sells_5m,
            },
        )

    def _insert_snapshot(
        self,
        snapshot: Snapshot,
        score: Score,
        *,
        passes_filter: bool,
        now: datetime,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO snapshots (
                recorded_at, pair_address, chain, symbol, score,
                volume_acceleration, buy_pressure, transaction_acceleration,
                price_change, penalty, liquidity_usd, market_cap,
                volume_5m, volume_1h, buys_5m, sells_5m, txns_5m, txns_1h,
                price_change_5m, passes_filter
            ) VALUES (
                :recorded_at, :pair_address, :chain, :symbol, :score,
                :volume_acceleration, :buy_pressure, :transaction_acceleration,
                :price_change, :penalty, :liquidity_usd, :market_cap,
                :volume_5m, :volume_1h, :buys_5m, :sells_5m, :txns_5m, :txns_1h,
                :price_change_5m, :passes_filter
            )
            """,
            {
                "recorded_at": now.isoformat(timespec="seconds"),
                "pair_address": snapshot.pair_address,
                "chain": snapshot.chain,
                "symbol": snapshot.symbol,
                "score": score.total,
                "volume_acceleration": score.volume_acceleration,
                "buy_pressure": score.buy_pressure,
                "transaction_acceleration": score.transaction_acceleration,
                "price_change": score.price_change,
                "penalty": score.penalty,
                "liquidity_usd": snapshot.liquidity_usd,
                "market_cap": snapshot.market_cap,
                "volume_5m": snapshot.volume_5m,
                "volume_1h": snapshot.volume_1h,
                "buys_5m": snapshot.buys_5m,
                "sells_5m": snapshot.sells_5m,
                "txns_5m": snapshot.txns_5m,
                "txns_1h": snapshot.txns_1h,
                "price_change_5m": snapshot.price_change_5m,
                "passes_filter": 1 if passes_filter else 0,
            },
        )

    def _append_alert(self, alert: Alert) -> None:
        self.alerts_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(alert.model_dump(mode="json"), separators=(",", ":"))
        with self.alerts_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
