"""DexScreener parsers and a mocked poll. No network."""

from datetime import datetime, timezone

import httpx

from trench_momentum.sources.dexscreener import (
    DexScreenerClient,
    collect_solana_snapshots,
    parse_pair,
    parse_token_profiles,
    select_deepest_pair,
)


def _pair(
    *,
    address: str,
    symbol: str,
    liquidity: float | None,
    base: str,
    created_ms: int = 1_700_000_000_000,
) -> dict:
    return {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": address,
        "baseToken": {"address": base, "name": symbol, "symbol": symbol},
        "priceUsd": "0.01",
        "txns": {
            "m5": {"buys": 40, "sells": 10},
            "h1": {"buys": 80, "sells": 20},
        },
        "volume": {"m5": 5000, "h1": 9000},
        "priceChange": {"m5": 12.5},
        "liquidity": None if liquidity is None else {"usd": liquidity},
        "fdv": 250000,
        "marketCap": 180000,
        "pairCreatedAt": created_ms,
    }


def test_profiles_keep_solana_only():
    profiles = parse_token_profiles(
        [
            {"chainId": "solana", "tokenAddress": "MintA", "url": "https://dexscreener.com/solana/a"},
            {"chainId": "ethereum", "tokenAddress": "0xabc"},
            {"chainId": "Solana", "tokenAddress": "MintA"},
            {"chainId": "solana", "tokenAddress": "MintB"},
            {"chainId": "solana"},
        ]
    )

    assert [(item.chain_id, item.token_address) for item in profiles] == [
        ("solana", "MintA"),
        ("solana", "MintB"),
    ]


def test_select_deepest_base_pair():
    chosen = select_deepest_pair(
        [
            _pair(address="quote-pool", symbol="OTHER", liquidity=90_000, base="OtherMint"),
            _pair(address="thin", symbol="MINT", liquidity=1_000, base="MintA"),
            _pair(address="deep", symbol="MINT", liquidity=40_000, base="MintA"),
            _pair(address="unknown", symbol="MINT", liquidity=None, base="MintA"),
        ],
        token_address="MintA",
    )

    assert chosen is not None
    assert chosen["pairAddress"] == "deep"


def test_select_skips_missing_or_zero_liquidity():
    assert (
        select_deepest_pair(
            [
                _pair(address="missing", symbol="MINT", liquidity=None, base="MintA"),
                _pair(address="zero", symbol="MINT", liquidity=0, base="MintA"),
            ],
            token_address="MintA",
        )
        is None
    )
    chosen = select_deepest_pair(
        [
            _pair(address="zero", symbol="MINT", liquidity=0, base="MintA"),
            _pair(address="funded", symbol="MINT", liquidity=12, base="MintA"),
        ],
        token_address="MintA",
    )
    assert chosen is not None
    assert chosen["pairAddress"] == "funded"


def test_collect_uses_profile_and_pair_endpoints_only():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.dexscreener.com"
        seen.append(request.url.path)
        if request.url.path == "/token-profiles/latest/v1":
            return httpx.Response(
                200,
                json=[
                    {"chainId": "ethereum", "tokenAddress": "0xeth"},
                    {"chainId": "solana", "tokenAddress": "MintA"},
                    {"chainId": "solana", "tokenAddress": "MintBroken"},
                ],
            )
        if request.url.path == "/token-pairs/v1/solana/MintA":
            return httpx.Response(
                200,
                json=[
                    _pair(address="thin", symbol="MINT", liquidity=1_000, base="MintA"),
                    _pair(address="deep", symbol="MINT", liquidity=40_000, base="MintA"),
                ],
            )
        if request.url.path == "/token-pairs/v1/solana/MintBroken":
            return httpx.Response(500, json={"error": "upstream"})
        raise AssertionError(f"unexpected request {request.url}")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http:
        with DexScreenerClient(http=http) as client:
            batch = collect_solana_snapshots(client)

    assert seen == [
        "/token-profiles/latest/v1",
        "/token-pairs/v1/solana/MintA",
        "/token-pairs/v1/solana/MintBroken",
    ]
    assert batch.skipped == 1
    assert len(batch.snapshots) == 1
    snapshot = batch.snapshots[0]
    assert snapshot.pair_address == "deep"
    assert snapshot.symbol == "MINT"
    assert snapshot.liquidity_usd == 40_000
    assert snapshot.volume_5m == 5000
    assert snapshot.volume_1h == 9000
    assert snapshot.buys_5m == 40
    assert snapshot.sells_5m == 10
    assert snapshot.txns_1h == 100
    assert snapshot.price_change_5m == 12.5
    assert snapshot.fdv == 250000
    assert snapshot.market_cap == 180000
    assert snapshot.pair_created_at == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


def test_pumpfun_null_liquidity_is_curve_not_scored():
    curve_pair = _pair(address="curve-pair", symbol="CURVE", liquidity=None, base="MintC")
    curve_pair["dexId"] = "pumpfun"
    funded = _pair(address="amm-pair", symbol="CURVE", liquidity=25_000, base="MintC")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token-profiles/latest/v1":
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": "MintC"}])
        if request.url.path == "/token-pairs/v1/solana/MintC":
            return httpx.Response(200, json=[curve_pair, funded])
        raise AssertionError(f"unexpected request {request.url}")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http:
        with DexScreenerClient(http=http) as client:
            batch = collect_solana_snapshots(client)

    assert batch.curve == ()
    assert [item.pair_address for item in batch.snapshots] == ["amm-pair"]
    assert batch.zero_liquidity == ()


def test_curve_only_token_keeps_five_minute_flow():
    curve_pair = _pair(address="curve-pair", symbol="CURVE", liquidity=None, base="MintC")
    curve_pair["dexId"] = "pumpfun"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token-profiles/latest/v1":
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": "MintC"}])
        if request.url.path == "/token-pairs/v1/solana/MintC":
            return httpx.Response(200, json=[curve_pair])
        raise AssertionError(f"unexpected request {request.url}")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http:
        with DexScreenerClient(http=http) as client:
            batch = collect_solana_snapshots(client)

    assert batch.snapshots == []
    assert len(batch.curve) == 1
    assert batch.curve[0].buys_5m == 40
    assert batch.curve[0].sells_5m == 10
    assert batch.curve[0].pair_created_at == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
    assert batch.curve[0].chain == "solana"


def test_missing_market_cap_uses_fdv_for_the_filter_field():
    snapshot = parse_pair(
        {
            "chainId": "solana",
            "pairAddress": "fdv-only",
            "baseToken": {"symbol": "FDV"},
            "fdv": 900_000,
            "liquidity": {"usd": 20_000},
            "volume": {"m5": 1, "h1": 1},
            "txns": {"m5": {"buys": 1, "sells": 1}},
            "priceChange": {"m5": 0},
            "pairCreatedAt": 1_700_000_000_000,
        }
    )

    assert snapshot.market_cap == 900_000
    assert snapshot.fdv == 900_000
