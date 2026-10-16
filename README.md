# trench-momentum

CLI that scores onchain momentum for low-cap tokens. Not a trading bot.

It does not hold a wallet, sign a swap, or store a private key. v1 reads public DexScreener pair data for Solana, scores each pair locally, and writes an alert only when that score crosses a threshold.

## Formula

`momentum_score` is a pure function. Each term is clamped to `[0, 1]` and then weighted. Do not change it unless asked.

| Term | Weight | Raw value, then clamp to [0, 1] |
| --- | ---: | --- |
| Volume acceleration | 30 | `volume_5m / (volume_1h / 12)` |
| Buy pressure | 25 | `buys_5m / (buys_5m + sells_5m)` |
| Transaction acceleration | 20 | `txns_5m / (txns_1h / 12)` |
| Five-minute price change | 15 | `price_change_5m / 50` |

`price_change_5m` is a percent. A move of +50% or more saturates the 15-point term. A negative print scores 0.

The penalty is 20 points, not 40, when either of these is true:

- `volume_5m / liquidity_usd > 20`
- `liquidity_usd < 10000`

A ratio of exactly 20, or liquidity of exactly 10000, does not penalize. The positive weights sum to 90. The total is clamped to `[0, 100]`.

Volume acceleration is 0 when `volume.h1` is 0. Transaction acceleration is 0 when the one-hour transaction count is 0. An empty baseline does not saturate either term. No buys and no sells means buy pressure is 0.

A ratio of 1 means the last five minutes are keeping pace with the trailing hour. Faster than that still scores 1 on that term.

## Curve score

`pumpfun` rows whose `liquidity` is null use `curve_score` instead. It does not use the liquidity penalty, and it does not change the AMM weights above. Each term is clamped to `[0, 1]`.

| Term | Weight | Raw value, then clamp to [0, 1] |
| --- | ---: | --- |
| Volume acceleration | 40 | `volume_5m / (volume_1h / 12)` |
| Buy pressure | 40 | `buys_5m / (buys_5m + sells_5m)` over 5 minutes |
| Absolute 5-minute volume | 20 | `volume_5m / 20000` |

If `volume.h1` is 0, volume acceleration is 0. If buys plus sells over 5 minutes is under 20, the score is 0. The total is clamped to `[0, 100]`.

## v1 filters

Pairs outside this set are still scored and stored, and they do not alert:

- chain is `solana`
- liquidity `>= 10000` USD
- market cap `<= 3000000` USD (missing market cap is excluded)
- pair age `<= 48h` (missing creation time is excluded)

## Alerts

The default threshold is 60. Each poll appends one sqlite row per AMM pair to `data/snapshots.sqlite`, except pairs younger than 30 minutes. Those stay on the table with alert `age` and are not written to `data/alerts.jsonl`. `data/alerts.jsonl` gets an AMM line, with `lane` set to `amm`, only when a pair inside the filters, at least 30 minutes old, crosses from below the threshold to at or above it. The first in-universe print at or above 60 counts as a cross. A later poll that is still above 60 does not alert again. The score has to come back below 60 on an in-universe poll before the next cross is written.

Curve rows use their own sqlite table and their own latch. A curve line is written only on a fresh cross of 60, and that line has `lane` set to `curve`. A curve row younger than 30 minutes is printed with alert `age` and is not written. A curve row with no creation time is printed and is not alerted.

## Run the tests

Python 3.12. Tests use the packaged fixture and in-memory inputs. They do not call DexScreener.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Run the offline demo

The default command reads `src/trench_momentum/fixtures/offline_pairs.json` and does not open a socket.

```bash
python -m trench_momentum
```

The table is sorted by score. In the fixture, GIANT is above the market-cap cap, DUST is under the liquidity floor, and STALE is older than 48h, so none of those alert. FLAT stays under 60. MOMO and WASH cross 60 and are written to `data/alerts.jsonl`. Run the same command again and those two are not repeated.

`ageHours` in the fixture is local to the demo. The loader turns it into a creation timestamp relative to the clock at startup, then the DexScreener parser builds the snapshot.

## Live poll

No API key. The live command does not scrape the DexScreener website.

```bash
python -m trench_momentum live --once
python -m trench_momentum live
```

`live --once` does one pass and prints the AMM table and the curve table. `live` repeats that pass every 20 seconds until Ctrl+C. Both use the same AMM filters, the same 30-minute age gate, and the same threshold of 60. sqlite records every scored AMM pair that is at least 30 minutes old, and every scored curve row that has a creation time and is at least 30 minutes old. `alerts.jsonl` gets an AMM line only when a pair inside the filters freshly crosses 60. A curve line uses the same threshold and age gate, with `lane` set to `curve`.

Discovery is `GET /token-profiles/latest/v1` (60 requests per minute). Only `chainId == solana` is kept. That endpoint is the latest token profile pages. It is not a new-pair feed: a profile can be updated on a pool that is already older than 48h, and a brand-new pool with no profile never shows up here.

Each Solana address is then enriched with `GET /token-pairs/v1/solana/{tokenAddress}` (300 requests per minute). Pools whose `liquidity.usd` is missing or 0 are not AMM-scored and are not filter rejects. A `pumpfun` pool with null `liquidity` is scored on the curve table. If that token also has an AMM pool with `liquidity.usd`, the curve row is dropped. Among the pools that remain, the client picks the highest `liquidity.usd`. Pools whose base token is that address are preferred over a deeper pool where the token is only the quote. The snapshot uses `priceChange.m5`, `volume.m5`, `volume.h1`, `txns.m5` buys and sells, `txns.h1` buys and sells (for transaction acceleration), `liquidity.usd`, `fdv`, and `pairCreatedAt`. `pairCreatedAt` is unix milliseconds. The age filter uses that timestamp. `marketCap` is the market-cap filter when DexScreener sends it; otherwise `fdv` fills that field.
