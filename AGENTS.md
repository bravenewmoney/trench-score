# trench-momentum

CLI that scores onchain momentum for low-cap tokens. Not a trading bot.

## Scope

- Python 3.12
- No web framework, no wallet, no swap, no private key
- v1 polls DexScreener only, Solana chain
- Alert only when the score crosses the threshold, not on every poll

## Order

1. This file exists before any app code.
2. score.py is a pure function with tests, before any API client.
3. Do not change the formula unless explicitly asked.

## Formula

Score 0-100:

- 30 volume acceleration (5m volume vs 1h volume / 12)
- 25 buy pressure (buys / (buys + sells) over 5m)
- 20 transaction acceleration
- 15 five-minute price change, capped
- minus up to 20 if 5m volume / liquidity > 20 or liquidity is below the floor

## v1 filters

- chain = solana
- liquidity >= 10000 USD
- market cap <= 3_000_000
- age <= 48h

## Do not

- Do not add FastAPI, Redis, a frontend, or ML
- Do not commit tokens, keys, or webhooks
- Do not re-alert the same pair until the score falls below the threshold and crosses it again
- All code, comments, commit messages, and docs in English
