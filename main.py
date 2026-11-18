#!/usr/bin/env python3
"""Dip-bounce BTC trading bot for the make.fun BTC race.

Speaks agent-fun/bars-replay/v1 over NDJSON on stdin/stdout: the harness
sends one ``hello`` line, then one ``batch`` line per bar (each carrying the
bar that just closed, the fills of the orders sent on the previous batch,
and the account), then ``done``. This bot answers every batch with exactly
one ``reply`` line echoing its seq and carrying its orders.

Strategy (long-only, one position at a time, full-size entries):
  * Entry: buy when the latest close sits at least 60 bps below its
    120-bar mean (or at least 150 bps below its 120-bar high) AND the
    price has just turned up for two bars in a row. The confirmation
    matters: in a falling market the dip alone keeps dipping.
  * Exit: sell at +220 bps over the entry fill, on a -50 bps stop, or
    when the price has pulled back 100 bps from its peak since entry
    while still at least +20 bps in profit (trailing exit).
  * No new entries in the last 180 bars of the window, so a position is
    never opened with no time left to reach its target.
  * No trading during warm-up bars.

Everything is integer arithmetic on the protocol's scaled integers, seeded
only by the data in the hello/batch messages: no wall clock, no
environment, no randomness, so two runs produce identical action logs.
"""

import json
import sys

PROTOCOL = "agent-fun/bars-replay/v1"
SYMBOL = "CBBTC"

# --- strategy parameters (basis points unless noted) -----------------------
MEAN_BARS = 120          # bars in the reference mean / high window
DIP_BPS = 60             # entry A: close this far below the 120-bar mean
DRAWDOWN_BPS = 150       # entry B: close this far below the 120-bar high
TARGET_BPS = 220         # take profit over the entry fill price
STOP_BPS = 50            # stop loss under the entry fill price
TRAIL_BPS = 100          # trailing exit: pullback from the peak since entry
TRAIL_ARM_BPS = 20       # trailing exit only once this far in profit
END_GUARD_BARS = 180     # no new entries this close to the window's end
WINDOW_BARS = 1440       # one-minute bars in a 24h window (per the rules)
MAX_SLIPPAGE_BPS = 300   # generous cap vs the ~7 bps typical 1-bar move

BPS = 10_000


class Bot:
    def __init__(self, hello):
        self.seed = hello.get("seed", "0")
        self.warmup_bars = int(hello.get("warmup_bars", 0) or 0)
        symbols = {s["symbol"]: s for s in hello.get("symbols", [])}
        self.symbol = symbols.get(SYMBOL, {})
        self.price_scale = int(self.symbol.get("price_scale", 5))
        self.qty_scale = int(self.symbol.get("qty_scale", 8))
        self.quote_scale = int(hello.get("capital", {}).get("quote_scale", 6))
        fees = hello.get("fees", {})
        self.min_notional = int(fees.get("min_notional_quote_units", "1000000"))
        caps = hello.get("caps", {})
        self.max_weight_bps = int(caps.get("max_weight_bps", 10000))
        self.max_orders = int(caps.get("max_orders_per_bar", 16))
        # market state, updated from every batch
        self.closes = []            # closes of CBBTC bars seen, oldest first
        self.cash = int(hello.get("capital", {}).get("quote_units", "0"))
        self.equity = self.cash
        self.position_qty = 0       # qty held, in qty_scale units
        self.entry_price = None     # fill price of the open position
        self.peak_price = None      # highest close since entry
        self.order_no = 0           # deterministic client-id counter

    # -- helpers ------------------------------------------------------------
    def _next_client_id(self, prefix):
        self.order_no += 1
        return "%s%d" % (prefix, self.order_no)

    def _mean_before_last(self):
        """Mean of the MEAN_BARS closes before the latest one."""
        window = self.closes[-MEAN_BARS - 1:-1]
        return sum(window) // len(window)

    def _high_before_last(self):
        return max(self.closes[-MEAN_BARS - 1:-1])

    def _rising_two_bars(self):
        c = self.closes
        return len(c) >= 3 and c[-1] > c[-2] >= c[-3]

    # -- batch handling -----------------------------------------------------
    def on_batch(self, batch):
        seq = batch["seq"]
        # Fold in this batch's bar (CBBTC may be absent on a gap minute).
        for bar in batch.get("bars", []):
            if bar.get("symbol") == SYMBOL:
                self.closes.append(int(bar["c"]))
        # Fold in fills: a filled buy sets the entry, a filled sell clears it.
        for fill in batch.get("fills", []):
            if fill.get("symbol") != SYMBOL or fill.get("status") != "filled":
                continue
            if fill.get("side") == "buy":
                self.entry_price = int(fill["price"])
                self.peak_price = self.entry_price
            elif fill.get("side") == "sell":
                self.entry_price = None
                self.peak_price = None
        # The account is authoritative for cash and position.
        account = batch.get("account", {})
        self.cash = int(account.get("cash_quote_units", self.cash))
        self.equity = int(account.get("equity_quote_units", self.equity))
        self.position_qty = 0
        for pos in account.get("positions", []):
            if pos.get("symbol") == SYMBOL:
                self.position_qty = int(pos["qty"])
                if self.entry_price is None:
                    # Defensive fallback: derive the cost basis from the
                    # position's recorded cost if no buy fill was seen.
                    cost = int(pos["cost_quote_units"])
                    if self.position_qty > 0 and cost > 0:
                        self.entry_price = (
                            cost * 10 ** (self.qty_scale + self.price_scale)
                            // (self.position_qty * 10 ** self.quote_scale)
                        )
                        self.peak_price = self.entry_price
        if self.position_qty == 0:
            self.entry_price = None
            self.peak_price = None
        elif self.peak_price is not None and self.closes:
            if self.closes[-1] > self.peak_price:
                self.peak_price = self.closes[-1]
        return self.decide(seq)

    def decide(self, seq):
        orders = []
        if seq < self.warmup_bars:
            return orders  # warm-up bars: build history, never trade
        if not self.closes or len(self.closes) < MEAN_BARS + 1:
            return orders  # not enough history for the reference window
        close = self.closes[-1]
        scored_idx = seq - self.warmup_bars

        if self.position_qty == 0:
            if scored_idx >= WINDOW_BARS - END_GUARD_BARS:
                return orders  # too late in the window to open a trade
            mean = self._mean_before_last()
            high = self._high_before_last()
            dip = close * BPS < mean * (BPS - DIP_BPS)
            drawdown = close * BPS < high * (BPS - DRAWDOWN_BPS)
            if (dip or drawdown) and self._rising_two_bars():
                # Full-size entry, capped by the weight limit (with no
                # position held, the room is the weight cap on equity).
                room = self.max_weight_bps * self.equity // BPS
                notional = min(self.cash, room)
                if notional >= self.min_notional and notional > 0:
                    orders.append(
                        {
                            "side": "buy",
                            "client_id": self._next_client_id("b"),
                            "symbol": SYMBOL,
                            "notional_quote_units": str(notional),
                            "max_slippage_bps": MAX_SLIPPAGE_BPS,
                        }
                    )
        else:
            entry = self.entry_price
            if entry is None or entry <= 0:
                return orders
            gain_bps = close * BPS // entry - BPS
            peak = self.peak_price if self.peak_price else entry
            pullback_bps = (peak - close) * BPS // peak if peak > 0 else 0
            sell = False
            if gain_bps >= TARGET_BPS:
                sell = True  # take profit
            elif gain_bps <= -STOP_BPS:
                sell = True  # stop loss
            elif gain_bps > TRAIL_ARM_BPS and pullback_bps >= TRAIL_BPS:
                sell = True  # trailing exit after a run-up
            if sell and self.position_qty > 0:
                orders.append(
                    {
                        "side": "sell",
                        "client_id": self._next_client_id("s"),
                        "symbol": SYMBOL,
                        "qty": str(self.position_qty),
                        "max_slippage_bps": MAX_SLIPPAGE_BPS,
                    }
                )
        return orders[: self.max_orders]


def main():
    bot = None
    out = sys.stdout
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            return 1
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "hello":
            if msg.get("protocol") != PROTOCOL:
                return 1
            bot = Bot(msg)
        elif kind == "batch":
            if bot is None:
                return 1
            orders = bot.on_batch(msg)
            reply = {"type": "reply", "seq": msg["seq"], "orders": orders}
            out.write(json.dumps(reply, separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            return 0
        else:
            return 1
    return 0 if bot is not None else 1


if __name__ == "__main__":
    sys.exit(main())
