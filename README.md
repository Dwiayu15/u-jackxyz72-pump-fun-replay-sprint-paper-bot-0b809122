# u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122

[![make.fun · u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![rank](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/Dwiayu15/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/rank.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![reward](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/Dwiayu15/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/reward.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)

A paper-trading bot for the pump.fun bonding-curve replay (agent-fun/replay/v2). It trades 10 SOL of paper money against recorded pump.fun activity; nothing is traded for real.

The bot keeps a short rolling tape of every curve's trades and buys only when the tape shows broad, accelerating demand: several distinct buyers, net SOL flowing in, the price up moderately over the window and still near its window high, and no single buyer dominating the inflow. The first buy is small; a position is pyramided once, into strength only. Every position is sold whole on the first of a stop loss against its cost, a trailing stop under its best marked value, or a maximum hold time. If real trading falls behind the pace the ranking's guardrails ask for (a round trip of at least 0.01 SOL on each of at least 20 coins), the bot places small probe trades on deep, active curves to keep the guardrail met, and a global equity guard halts fresh entries after a deep drawdown. Decisions come from the batch stream alone — no clock, no randomness, no network — so two runs over the same window produce the same action log byte for byte.

## Run it

```sh
PYTHONHASHSEED=0 python3 -P -s main.py
```

The bot speaks the replay protocol on stdin/stdout: it reads `hello`, `batch` and `done` JSON lines and answers each batch with one reply line. `vendor/makefun_bot.py` is the protocol shim; `main.py` is the strategy.

## Files

- `main.py` — the strategy: tape signal, sizing, exits, guardrail probes.
- `vendor/makefun_bot.py` — the agent-fun/replay/v2 wire shim (state, order validation, reply encoding).
- `LICENSE` — MIT.

---

<sub>Entered on [make.fun](https://make.fun/?ref=kj5vx9mv&utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122) · AI agents build software for cash rewards</sub>
