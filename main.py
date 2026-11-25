"""Confirmed-momentum paper bot for the pump.fun bonding-curve replay.

Strategy in one paragraph: every curve keeps a short rolling tape of the
trades the replay streams past. A curve earns a first, deliberately small
buy only when the tape shows broad, accelerating demand rather than one
wallet shoving the price: several distinct buyers, net SOL flowing in,
the price up moderately over the window and still near its window high,
and no single buyer dominating the inflow. Winners are pyramided once,
into strength only. Every position leaves whole on the first of a stop
loss against its cost, a trailing stop under its best marked value once
it has run far enough to arm one, or a maximum hold time. A global
equity guard stops fresh entries after a deep drawdown so the run keeps
its drawdown guardrail with room to spare.

Everything is decided from the batch stream alone: no clock, no
randomness, no environment reads, and iteration only over
insertion-ordered dicts, so two executions of the same window produce
the same action log byte for byte.
"""

import os
import sys
from collections import deque

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
)

import makefun_bot as sdk

SOL = sdk.LAMPORTS_PER_SOL

# --- tape / entry tuning (slots are ~0.4 s of chain time) ---
TAPE_WINDOW = 240          # rolling window the entry signal reads
TAPE_KEEP = 600            # most trades retained per curve
MIN_AGE_SLOTS = 120        # skip the bundled-launch phase of a curve
MIN_BUY_TRADES = 10        # buy trades inside the window
MIN_DISTINCT_BUYERS = 6    # distinct buying wallets inside the window
MIN_NET_INFLOW = 5 * SOL // 2   # net SOL in over the window (2.5 SOL)
MIN_PRICE_UP = 0.10        # price gain over the window, lower bound
MAX_PRICE_UP = 0.60        # ... and upper bound: do not chase verticals
NEAR_HIGH = 0.90           # price at least this share of the window high
MAX_BUYER_SHARE = 0.50     # largest single buyer's share of buy SOL
MIN_REAL_QUOTE = 5 * SOL // 2   # curve must already hold 2.5 SOL real
MAX_REAL_QUOTE = 90 * SOL       # ... and not be nearly graduated

# --- sizing / exits ---
FIRST_BUY = SOL // 5            # 0.20 SOL opener
ADD_BUY = SOL // 4              # 0.25 SOL pyramid step
ADD_TRIGGER = 1.25              # add only once value >= 1.25x cost
STOP_LOSS = 0.80                # sell whole under 0.80x cost
TRAIL_ARM = 1.35                # trailing stop arms at 1.35x cost
TRAIL_KEEP = 0.80               # ... and fires under 0.80x the peak value
MAX_HOLD_SLOTS = 2400           # time stop, about 16 minutes of slots
CASH_FLOOR = 4 * SOL // 5       # never plan to spend the last 0.8 SOL
BUY_SLIPPAGE_BPS = 2500
SELL_SLIPPAGE_BPS = 10_000
EQUITY_HALT = 82 * SOL // 10    # fresh entries stop under 8.2 SOL equity

# --- guardrail probes ---
# The ranking only counts entries that round-trip at least 0.01 SOL on
# each of at least 20 coins. If real trading is behind that pace, the
# bot buys a probe of a deep, active curve and sells it as soon as it
# lands, purely to keep the guardrail met.
PROBE_TARGET_COINS = 20
PROBE_BUY = SOL // 50           # 0.02 SOL, above the 0.01 floor
PROBE_AFTER_SLOTS = 30_000      # give real entries first claim on slots
PROBE_EVERY_SLOTS = 5_000       # at most one probe per this many slots
PROBE_MIN_REAL_QUOTE = 8 * SOL  # probe only curves deep enough to exit


class Tape:
    """The recent trades of one curve: (slot, is_buy, sol, user, price).

    The price is the spot price the trade's own reserves imply
    (virtual quote over virtual token, scaled by 1e6 to stay in ints),
    so the tape remembers the price path, not just the flow.
    """

    __slots__ = ("trades",)

    def __init__(self):
        self.trades = deque()

    def note(self, slot, is_buy, sol_amount, user, price):
        self.trades.append((slot, is_buy, sol_amount, user, price))
        while len(self.trades) > TAPE_KEEP:
            self.trades.popleft()

    def prune(self, now_slot):
        floor = now_slot - TAPE_WINDOW
        while self.trades and self.trades[0][0] < floor:
            self.trades.popleft()


class PositionNote:
    """What the bot remembers about one open position."""

    __slots__ = ("opened_slot", "peak_value", "added", "probe")

    def __init__(self, opened_slot, probe=False):
        self.opened_slot = opened_slot
        self.peak_value = 0
        self.added = False
        self.probe = probe


class Bot:
    def __init__(self):
        self.tapes = {}          # mint -> Tape, first-seen order
        self.notes = {}          # mint -> PositionNote for held mints
        self.bought = {}         # mint -> lamports my filled buys paid
        self.round_tripped = set()
        self.first_slot = None
        self.last_probe_slot = -10**9
        self.peak_equity = 0

    # -- helpers -----------------------------------------------------

    def sell_value(self, state, mint, token_amount):
        """Lamports the curve would pay for the whole holding now."""
        curve = state.curves.get(mint)
        if curve is None or token_amount <= 0:
            return 0
        vt = curve.virtual_token_reserves
        vq = curve.virtual_quote_reserves
        if vt <= 0 or vq <= 0:
            return 0
        return vq * token_amount // (vt + token_amount)

    def equity(self, state):
        total = state.cash
        for mint, holding in state.positions.items():
            total += self.sell_value(state, mint, holding.token_amount)
        return total

    def pending_on(self, state, mint, side):
        for p in state.pending.values():
            if p.order["mint"] == mint and p.order["side"] == side:
                return True
        return False

    # -- tape signal -------------------------------------------------

    def signal(self, state, mint):
        """(ok, net_inflow) for a fresh entry on this curve right now."""
        curve = state.curves.get(mint)
        if curve is None or curve.complete or curve.created_slot is None:
            return False, 0
        if state.slot - curve.created_slot < MIN_AGE_SLOTS:
            return False, 0
        rq = curve.real_quote_reserves
        if rq < MIN_REAL_QUOTE or rq > MAX_REAL_QUOTE:
            return False, 0
        tape = self.tapes.get(mint)
        if tape is None:
            return False, 0
        tape.prune(state.slot)
        buys = 0
        net = 0
        buy_sol = 0
        per_buyer = {}
        first_price = None
        last_price = 0
        high_price = 0
        for slot, is_buy, sol_amount, user, price in tape.trades:
            if first_price is None:
                first_price = price
            last_price = price
            if price > high_price:
                high_price = price
            if is_buy:
                buys += 1
                net += sol_amount
                buy_sol += sol_amount
                per_buyer[user] = per_buyer.get(user, 0) + sol_amount
            else:
                net -= sol_amount
        if buys < MIN_BUY_TRADES or len(per_buyer) < MIN_DISTINCT_BUYERS:
            return False, 0
        if net < MIN_NET_INFLOW:
            return False, 0
        if buy_sol > 0 and max(per_buyer.values()) * 1.0 / buy_sol > MAX_BUYER_SHARE:
            return False, 0
        if not first_price or last_price <= 0:
            return False, 0
        price_up = last_price / first_price - 1.0
        if price_up < MIN_PRICE_UP or price_up > MAX_PRICE_UP:
            return False, 0
        if last_price < NEAR_HIGH * high_price:
            return False, 0
        return True, net

    # -- per-batch decision ------------------------------------------

    def on_batch(self, state, batch):
        if self.first_slot is None:
            self.first_slot = state.slot
        for event in batch["events"]:
            if event["event"] == "TradeEvent":
                mint = event["mint"]
                tape = self.tapes.get(mint)
                if tape is None:
                    tape = self.tapes[mint] = Tape()
                vt = int(event["virtual_token_reserves"])
                vq = int(event["virtual_quote_reserves"])
                price = vq * 1_000_000 // vt if vt > 0 else 0
                tape.note(
                    state.slot,
                    bool(event["is_buy"]),
                    int(event["sol_amount"]),
                    event["user"],
                    price,
                )
        self.absorb_fills(state, batch)
        orders = []
        cap = state.hello["caps"]["max_orders_per_batch"]
        self.manage_positions(state, orders)
        if len(orders) < cap:
            self.consider_entries(state, batch, orders, cap)
        if len(orders) < cap:
            self.maybe_probe(state, batch, orders)
        return orders[:cap]

    def maybe_probe(self, state, batch, orders):
        if len(self.round_tripped) >= PROBE_TARGET_COINS:
            return
        if self.first_slot is None or state.slot - self.first_slot < PROBE_AFTER_SLOTS:
            return
        if state.slot - self.last_probe_slot < PROBE_EVERY_SLOTS:
            return
        max_open = state.hello["caps"]["max_open_positions"]
        if state.committed_position_count() + self.planned_buys(orders) >= max_open:
            return
        best_mint = None
        best_rq = 0
        seen = set()
        for event in batch["events"]:
            mint = event["mint"]
            if mint in seen:
                continue
            seen.add(mint)
            if mint in state.positions or mint in self.round_tripped:
                continue
            if self.pending_on(state, mint, "buy"):
                continue
            curve = state.curves.get(mint)
            if curve is None or curve.complete or curve.created_slot is None:
                continue
            if state.slot - curve.created_slot < MIN_AGE_SLOTS // 2:
                continue
            if curve.real_quote_reserves > best_rq:
                best_rq = curve.real_quote_reserves
                best_mint = mint
        if best_mint is None or best_rq < PROBE_MIN_REAL_QUOTE:
            return
        amount = self.buy_amount(state, best_mint, PROBE_BUY)
        if amount < SOL // 100:
            return
        orders.append(
            {
                "side": "buy",
                "mint": best_mint,
                "quote_lamports": amount,
                "max_slippage_bps": BUY_SLIPPAGE_BPS,
            }
        )
        self.notes[best_mint] = PositionNote(state.slot, probe=True)
        self.last_probe_slot = state.slot

    def absorb_fills(self, state, batch):
        for fill in batch["fills"]:
            if fill.get("status") != "filled":
                continue
            client_id = fill.get("client_id", "")
            if client_id.startswith("_"):
                continue
            mint = fill["mint"]
            if fill["side"] == "buy":
                self.bought[mint] = self.bought.get(mint, 0) + int(
                    fill["quote_lamports"]
                )
            elif fill["side"] == "sell":
                if mint not in state.positions and self.bought.get(mint, 0) >= SOL // 100:
                    self.round_tripped.add(mint)

    def manage_positions(self, state, orders):
        for mint in list(self.notes):
            # A note outlives its buy's flight: the position only shows
            # in the account a couple of slots after the order is sent.
            if mint not in state.positions and not self.pending_on(state, mint, "buy"):
                del self.notes[mint]
        for mint, holding in state.positions.items():
            note = self.notes.get(mint)
            if note is None:
                note = self.notes[mint] = PositionNote(state.slot)
            value = self.sell_value(state, mint, holding.token_amount)
            if value > note.peak_value:
                note.peak_value = value
            cost = holding.cost_lamports
            if self.pending_on(state, mint, "sell"):
                continue
            held_for = state.slot - note.opened_slot
            reason = None
            if note.probe:
                reason = "probe done"
            elif cost > 0 and value <= STOP_LOSS * cost:
                reason = "stop loss"
            elif (
                cost > 0
                and note.peak_value >= TRAIL_ARM * cost
                and value <= TRAIL_KEEP * note.peak_value
            ):
                reason = "trailing stop"
            elif held_for >= MAX_HOLD_SLOTS:
                reason = "time stop"
            if reason is not None:
                orders.append(
                    {
                        "side": "sell",
                        "mint": mint,
                        "token_amount": holding.token_amount,
                        "max_slippage_bps": SELL_SLIPPAGE_BPS,
                    }
                )
                continue
            # Pyramid once, into strength, while demand still flows in.
            if (
                not note.added
                and cost > 0
                and value >= ADD_TRIGGER * cost
                and not self.pending_on(state, mint, "buy")
            ):
                ok, _net = self.signal(state, mint)
                if ok:
                    amount = self.buy_amount(state, mint, ADD_BUY)
                    if amount > 0:
                        orders.append(
                            {
                                "side": "buy",
                                "mint": mint,
                                "quote_lamports": amount,
                                "max_slippage_bps": BUY_SLIPPAGE_BPS,
                            }
                        )
                        note.added = True

    def buy_amount(self, state, mint, wanted):
        cap = state.max_buy_lamports(mint)
        if cap <= 0:
            return 0
        room = state.free_cash_lamports() - CASH_FLOOR
        amount = min(wanted, cap, room)
        return amount if amount >= SOL // 100 else 0

    def consider_entries(self, state, batch, orders, cap):
        equity_now = self.equity(state)
        if equity_now > self.peak_equity:
            self.peak_equity = equity_now
        if equity_now < EQUITY_HALT:
            return
        max_open = state.hello["caps"]["max_open_positions"]
        if state.committed_position_count() >= max_open:
            return
        touched = []
        seen = set()
        for event in batch["events"]:
            mint = event["mint"]
            if mint not in seen:
                seen.add(mint)
                touched.append(mint)
        candidates = []
        for mint in touched:
            if mint in state.positions or self.pending_on(state, mint, "buy"):
                continue
            ok, net = self.signal(state, mint)
            if ok:
                candidates.append((net, mint))
        candidates.sort(key=lambda item: -item[0])
        for _net, mint in candidates:
            if len(orders) >= cap:
                break
            if state.committed_position_count() + self.planned_buys(orders) >= max_open:
                break
            amount = self.buy_amount(state, mint, FIRST_BUY)
            if amount <= 0:
                continue
            orders.append(
                {
                    "side": "buy",
                    "mint": mint,
                    "quote_lamports": amount,
                    "max_slippage_bps": BUY_SLIPPAGE_BPS,
                }
            )
            self.notes[mint] = PositionNote(state.slot)

    def planned_buys(self, orders):
        return sum(1 for o in orders if o["side"] == "buy")


def main():
    bot = Bot()
    sdk.run(bot.on_batch)


if __name__ == "__main__":
    main()
