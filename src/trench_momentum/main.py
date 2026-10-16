"""CLI entry. The default path scores a packaged fixture and does not use the network."""

import argparse
import json
import math
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from rich.text import Text

from trench_momentum.models import (
    DEFAULT_ALERT_THRESHOLD,
    Score,
    Snapshot,
    v1_rejection,
)
from trench_momentum.score import (
    BUY_WEIGHT,
    CURVE_ABSOLUTE_VOLUME_WEIGHT,
    CURVE_BUY_WEIGHT,
    CURVE_VOLUME_WEIGHT,
    PRICE_WEIGHT,
    TX_WEIGHT,
    VOLUME_WEIGHT,
    curve_score,
    momentum_score,
)
from trench_momentum.sources.dexscreener import (
    DexScreenerClient,
    PairDiagnostic,
    PollBatch,
    collect_solana_snapshots,
    parse_pair,
)
from trench_momentum.store import Store

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "offline_pairs.json"
POLL_INTERVAL_SECONDS = 20
MIN_ALERT_AGE = timedelta(minutes=30)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    console = _stdout_console()
    if args.command == "live":
        return _run_live(args, console)
    return _run_offline(args, console)


def _run_offline(args: argparse.Namespace, console: Console) -> int:
    now = datetime.now(timezone.utc)
    try:
        path = args.fixture or FIXTURE_PATH
        snapshots = load_offline_pairs(path, now)
    except (OSError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        Console(stderr=True).print(f"error: {exc}")
        return 1
    with Store(args.db, args.alerts) as store:
        _publish(
            console,
            store,
            snapshots,
            source=f"offline fixture {path.name}",
            threshold=args.threshold,
            alerts_path=args.alerts,
            now=now,
        )
    return 0


def _run_live(args: argparse.Namespace, console: Console) -> int:
    """One DexScreener pass, or a pass every 20s until Ctrl+C."""

    try:
        with DexScreenerClient() as client, Store(args.db, args.alerts) as store:
            while True:
                now = datetime.now(timezone.utc)
                try:
                    batch = collect_solana_snapshots(client)
                except (OSError, ValueError, httpx.HTTPError, ValidationError) as exc:
                    Console(stderr=True).print(f"error: {exc}")
                    if args.once:
                        return 1
                    if not _sleep(POLL_INTERVAL_SECONDS):
                        return 0
                    continue
                source = "DexScreener profiles"
                if batch.skipped:
                    source = f"{source} ({batch.skipped} skipped)"
                _print_pair_diagnostics(console, batch)
                _publish(
                    console,
                    store,
                    batch.snapshots,
                    source=source,
                    threshold=args.threshold,
                    alerts_path=args.alerts,
                    now=now,
                    title="AMM",
                )
                _publish_curve(
                    console,
                    store,
                    batch.curve,
                    threshold=args.threshold,
                    alerts_path=args.alerts,
                    now=now,
                )
                if args.once:
                    return 0
                console.print(f"next poll in {POLL_INTERVAL_SECONDS:.0f}s")
                if not _sleep(POLL_INTERVAL_SECONDS):
                    return 0
    except KeyboardInterrupt:
        return 0


def load_offline_pairs(path: Path, now: datetime) -> list[Snapshot]:
    """Load a fixture. ``ageHours`` is fixture-only and becomes ``pairCreatedAt``."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        raw_pairs = payload
    elif isinstance(payload, Mapping):
        raw_pairs = payload.get("pairs") or []
    else:
        raise ValueError("fixture must be a JSON object or list")
    if not isinstance(raw_pairs, list):
        raise ValueError("fixture field 'pairs' must be a list")
    snapshots: list[Snapshot] = []
    for item in raw_pairs:
        if not isinstance(item, Mapping):
            raise ValueError("fixture pair must be an object")
        snapshots.append(parse_pair(_apply_age_hours(dict(item), now)))
    return snapshots


def default_fixture_path() -> Path:
    return FIXTURE_PATH


def _apply_age_hours(raw: dict, now: datetime) -> dict:
    if "ageHours" in raw and not raw.get("pairCreatedAt"):
        created = now - timedelta(hours=float(raw["ageHours"]))
        raw["pairCreatedAt"] = int(created.timestamp() * 1000)
    raw.pop("ageHours", None)
    return raw


def _print_pair_diagnostics(console: Console, batch: PollBatch) -> None:
    """Print raw DexScreener fields. Empty liquidity is not a filter result."""

    for item in batch.diagnostics:
        console.print(
            f"{item.symbol}  dexId={item.dex_id}  pairAddress={item.pair_address}  "
            f"liquidity={json.dumps(item.liquidity)}  "
            f"volume.m5={json.dumps(item.volume_m5)}  volume.h1={json.dumps(item.volume_h1)}"
        )
    for item in batch.zero_liquidity:
        console.print(
            f"{item.symbol}  dexId={item.dex_id}  pairAddress={item.pair_address}  "
            f"liquidity={json.dumps(item.liquidity)}  "
            f"volume.m5={json.dumps(item.volume_m5)}  volume.h1={json.dumps(item.volume_h1)}  "
            "not scored"
        )


def _publish(
    console: Console,
    store: Store,
    snapshots: list[Snapshot],
    *,
    source: str,
    threshold: float,
    alerts_path: Path,
    now: datetime,
    title: str = "trench-momentum",
) -> None:
    rows: list[tuple[Snapshot, Score, str | None, str]] = []
    alert_count = 0
    scored: list[tuple[Snapshot, Score, str | None]] = []
    for snapshot in snapshots:
        score = momentum_score(snapshot)
        rejection = v1_rejection(snapshot, now)
        scored.append((snapshot, score, rejection))
    scored.sort(key=lambda item: (-item[1].total, item[0].symbol))
    for snapshot, score, rejection in scored:
        if _too_young_to_alert(snapshot, now):
            rows.append((snapshot, score, rejection, "age"))
            continue
        alerted = store.record(
            snapshot,
            score,
            passes_filter=rejection is None,
            threshold=threshold,
            now=now,
        )
        if alerted:
            alert_count += 1
        rows.append((snapshot, score, rejection, "cross" if alerted else ""))
    _render(
        console,
        rows,
        source=source,
        threshold=threshold,
        alert_count=alert_count,
        alerts_path=alerts_path,
        now=now,
        title=title,
    )


def _sleep(seconds: float) -> bool:
    """Return False when Ctrl+C interrupts the wait."""

    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        return False
    return True


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="trench-momentum",
        description=(
            "Score onchain momentum for low-cap Solana tokens. Not a trading bot. "
            "With no command, score the offline fixture. "
            "`live` polls DexScreener token profiles."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("live",),
        default=None,
        help="Omit to run the offline fixture. `live` polls DexScreener.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="With live, run one network pass and exit.",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help="Offline pairs JSON. Defaults to the packaged fixture.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_ALERT_THRESHOLD,
        help=f"Alert when the score crosses this value (default: {DEFAULT_ALERT_THRESHOLD:g}).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/snapshots.sqlite"),
        help="SQLite snapshot database (default: data/snapshots.sqlite).",
    )
    parser.add_argument(
        "--alerts",
        type=Path,
        default=Path("data/alerts.jsonl"),
        help="Alert log written only on a threshold cross (default: data/alerts.jsonl).",
    )
    args = parser.parse_args(argv)
    if args.command != "live" and args.once:
        parser.error("--once is only valid with the live command")
    if args.command == "live" and args.fixture is not None:
        parser.error("--fixture cannot be combined with live")
    return args


def _stdout_console() -> Console:
    """Keep the score table wide enough that symbols are not ellipsized.

    Rich ignores a width override on a non-terminal unless height is set too.
    """

    detected = Console().size.width
    return Console(width=max(detected, 160), height=48)


def _publish_curve(
    console: Console,
    store: Store,
    curve: tuple[PairDiagnostic, ...],
    *,
    threshold: float,
    alerts_path: Path,
    now: datetime,
) -> None:
    """Score pumpfun null-liquidity rows and alert on a fresh cross of the threshold.

    Rows younger than 30 minutes are printed with alert ``age`` and are not
    stored, so they do not arm the latch. A missing creation time is not
    treated as old enough to alert.
    """

    prepared: list[tuple[PairDiagnostic, object, str]] = []
    for item in curve:
        volume_5m = _non_negative_amount(item.volume_m5)
        volume_1h = _non_negative_amount(item.volume_h1)
        buys = item.buys_5m or 0
        sells = item.sells_5m or 0
        score = curve_score(
            volume_5m=volume_5m,
            volume_1h=volume_1h,
            buys_5m=buys,
            sells_5m=sells,
        )
        prepared.append((item, score, ""))
    prepared.sort(key=lambda row: (-row[1].total, row[0].symbol, row[0].pair_address))

    rows: list[tuple[PairDiagnostic, object, str]] = []
    alert_count = 0
    for item, score, _label in prepared:
        if item.pair_created_at is None:
            rows.append((item, score, ""))
            continue
        if _created_too_young(item.pair_created_at, now):
            rows.append((item, score, "age"))
            continue
        alerted = store.record_curve(
            pair_address=item.pair_address,
            chain=item.chain,
            symbol=item.symbol,
            score=score,
            volume_5m=_non_negative_amount(item.volume_m5),
            volume_1h=_non_negative_amount(item.volume_h1),
            buys_5m=item.buys_5m or 0,
            sells_5m=item.sells_5m or 0,
            market_cap=item.market_cap,
            price_change_5m=item.price_change_5m,
            threshold=threshold,
            now=now,
        )
        if alerted:
            alert_count += 1
        rows.append((item, score, "cross" if alerted else ""))
    _render_curve(
        console,
        rows,
        threshold=threshold,
        alert_count=alert_count,
        alerts_path=alerts_path,
        now=now,
    )


def _render_curve(
    console: Console,
    rows: list[tuple],
    *,
    threshold: float,
    alert_count: int,
    alerts_path: Path,
    now: datetime,
) -> None:
    table = Table(
        title="curve",
        caption=(
            f"pumpfun null liquidity  ·  no liquidity penalty  ·  threshold {threshold:g}  ·  "
            f"{alert_count} new alert(s) → {alerts_path}"
        ),
    )
    table.add_column("Symbol", style="bold", no_wrap=True)
    table.add_column("Score", justify="right", no_wrap=True)
    table.add_column("Vol", justify="right", no_wrap=True)
    table.add_column("Buy", justify="right", no_wrap=True)
    table.add_column("Abs", justify="right", no_wrap=True)
    table.add_column("Vol 5m", justify="right", no_wrap=True)
    table.add_column("Vol 1h", justify="right", no_wrap=True)
    table.add_column("Buys 5m", justify="right", no_wrap=True)
    table.add_column("Sells 5m", justify="right", no_wrap=True)
    table.add_column("Age", justify="right", no_wrap=True)
    table.add_column("Alert", no_wrap=True)
    if not rows:
        console.print(table)
        console.print("No curve pairs.")
        return
    for item, score, alert_label in rows:
        score_style = "bold green" if score.total >= threshold else ""
        if alert_label == "cross":
            alert_text = Text("cross", style="bold green")
        elif alert_label == "age":
            alert_text = Text("age", style="yellow")
        else:
            alert_text = Text("")
        table.add_row(
            item.symbol,
            Text(f"{score.total:.1f}", style=score_style),
            f"{CURVE_VOLUME_WEIGHT * score.volume_acceleration:.1f}",
            f"{CURVE_BUY_WEIGHT * score.buy_pressure:.1f}",
            f"{CURVE_ABSOLUTE_VOLUME_WEIGHT * score.absolute_volume:.1f}",
            _fmt_usd(_non_negative_amount(item.volume_m5)),
            _fmt_usd(_non_negative_amount(item.volume_h1)),
            _fmt_count(item.buys_5m),
            _fmt_count(item.sells_5m),
            _fmt_created_age(item.pair_created_at, now),
            alert_text,
        )
    console.print(table)


def _non_negative_amount(value: object) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return 0.0
    else:
        return 0.0
    if math.isnan(number) or math.isinf(number) or number < 0:
        return 0.0
    return number


def _render(
    console: Console,
    rows: list[tuple],
    *,
    source: str,
    threshold: float,
    alert_count: int,
    alerts_path: Path,
    now: datetime,
    title: str = "trench-momentum",
) -> None:
    table = Table(
        title=title,
        caption=(
            f"{source}  ·  threshold {threshold:g}  ·  "
            f"{alert_count} new alert(s) → {alerts_path}"
        ),
    )
    table.add_column("Symbol", style="bold", no_wrap=True)
    table.add_column("Score", justify="right", no_wrap=True)
    table.add_column("Vol", justify="right", no_wrap=True)
    table.add_column("Buy", justify="right", no_wrap=True)
    table.add_column("Tx", justify="right", no_wrap=True)
    table.add_column("Px", justify="right", no_wrap=True)
    table.add_column("Penalty", justify="right", no_wrap=True)
    table.add_column("Liquidity", justify="right", no_wrap=True)
    table.add_column("MCap", justify="right", no_wrap=True)
    table.add_column("Age", justify="right", no_wrap=True)
    table.add_column("Filter", no_wrap=True)
    table.add_column("Alert", no_wrap=True)

    if not rows:
        console.print(table)
        console.print("No pairs to score.")
        return

    for snapshot, score, rejection, alert_label in rows:
        score_style = "bold green" if score.total >= threshold else ""
        penalty_style = "red" if score.penalty else "dim"
        filter_text = Text("pass", style="green") if rejection is None else Text(rejection, style="yellow")
        if alert_label == "cross":
            alert_text = Text("cross", style="bold green")
        elif alert_label == "age":
            alert_text = Text("age", style="yellow")
        else:
            alert_text = Text("")
        table.add_row(
            snapshot.symbol,
            Text(f"{score.total:.1f}", style=score_style),
            f"{VOLUME_WEIGHT * score.volume_acceleration:.1f}",
            f"{BUY_WEIGHT * score.buy_pressure:.1f}",
            f"{TX_WEIGHT * score.transaction_acceleration:.1f}",
            f"{PRICE_WEIGHT * score.price_change:.1f}",
            Text(f"-{score.penalty:.0f}" if score.penalty else "0", style=penalty_style),
            _fmt_usd(snapshot.liquidity_usd),
            _fmt_usd(snapshot.market_cap),
            _fmt_age(snapshot, now),
            filter_text,
            alert_text,
        )
    console.print(table)


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}k"
    return f"${value:.0f}"


def _fmt_count(value: int | None) -> str:
    if value is None:
        return "—"
    return str(value)


def _too_young_to_alert(snapshot: Snapshot, now: datetime) -> bool:
    """Pairs younger than 30 minutes are printed and not alerted."""

    return _created_too_young(snapshot.pair_created_at, now)


def _created_too_young(created: datetime | None, now: datetime) -> bool:
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc) - created.astimezone(timezone.utc) < MIN_ALERT_AGE


def _fmt_age(snapshot: Snapshot, now: datetime) -> str:
    return _fmt_created_age(snapshot.pair_created_at, now)


def _fmt_created_age(created: datetime | None, now: datetime) -> str:
    if created is None:
        return "—"
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    hours = (moment.astimezone(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() / 3600
    if hours < 0:
        hours = 0
    return f"{round(hours)}h"


if __name__ == "__main__":
    raise SystemExit(main())
