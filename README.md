[![make.fun](https://make.fun/badge/btc-race-24h-20261118-1444.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![rank](https://make.fun/badge/btc-race-24h-20261118-1444/DKAnbu/btc-race-bot/rank.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![reward](https://make.fun/badge/btc-race-24h-20261118-1444/DKAnbu/btc-race-bot/reward.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)

# BTC Race Bot

A long-only BTC (cbBTC/USDC) trading bot for the make.fun bounty
[Race a BTC trading bot live for 24 hours](https://make.fun/bounty/btc-race-24h-20261118-1444).
It speaks the `agent-fun/bars-replay/v1` protocol (NDJSON on stdin/stdout):
the harness sends a `hello` line, then one `batch` per one-minute bar, and
the bot answers each batch with one `reply` carrying its `orders`.

## Strategy

The dev window falls 2.8% overall, so buy-and-hold fails the dev gate. This
bot trades the bounces instead, one full-size position at a time:

- **Entry** — buy when the latest close is at least 60 bps below its
  120-bar mean (or at least 150 bps below its 120-bar high) **and** the
  price has just turned up for two bars in a row. The two-bar confirmation
  is what keeps it from catching a falling knife.
- **Exit** — sell at +220 bps over the entry fill, on a −50 bps stop, or
  when the price pulls back 100 bps from its peak since entry while still
  at least +20 bps in profit (trailing exit).
- **Discipline** — no new entries in the last 180 bars of the window, so a
  position is never opened with no time left to reach its target; no
  trading during warm-up bars.

Everything is integer arithmetic on the protocol's scaled integers, with no
wall clock, no environment reads and no randomness, so two runs produce
byte-identical action logs.

## Grade locally

From the competition kit directory:

```sh
node cli/makefun.mjs grade-local --kit . --bot /path/to/btc-race-bot --driver docker
```

## Run

```sh
python3 -P -s main.py
```

Standard library only; no network access is needed or used.
