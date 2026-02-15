# Network

Raspberry Pi terminal tools — network dashboard, crypto trading bot, and buy signal alerts.

## Setup

```bash
pip3 install ccxt pandas ta rich psutil
```

## netdash.py

Full-screen terminal dashboard that monitors your home network in real-time.

```
python3 netdash.py
```

**Panels:**
- Interface status (IP, MAC, up/down)
- WiFi info (SSID, signal strength, channel)
- Live bandwidth with sparkline graphs (▁▂▃▅▇█)
- Connected devices on the network
- Active connections
- Network stats (bytes, packets, errors)

## trader.py

DOGE day trading bot with a live TUI dashboard. EMA crossover + RSI + volume strategy. Paper trading by default.

```
python3 trader.py
```

**Features:**
- EMA(9/21) crossover signals with RSI and volume confirmation
- Stop-loss (4%), take-profit (8%), daily loss limit ($10)
- Position sizing scales with signal confidence
- SQLite persistence — trades and portfolio survive restarts
- Paper trading by default — set `"live_trading": true` in `config.json` for real money

**Config** (`config.json`, auto-created on first run):

| Setting | Default | Description |
|---------|---------|-------------|
| `trading.pair` | `DOGE/USD` | Coin the bot trades |
| `trading.live_trading` | `false` | Paper mode by default |
| `risk.stop_loss_pct` | `0.04` | 4% stop-loss |
| `risk.take_profit_pct` | `0.08` | 8% take-profit |
| `risk.daily_loss_limit_usd` | `10.0` | Stop trading after $10 loss/day |

## alerts.py

Buy signal indicator that watches your crypto holdings for dip-buying opportunities.

```
python3 alerts.py
```

**Features:**
- Monitors XRP and SOL (configurable) for buy-the-dip signals
- Tracks your holdings and their USD value
- RSI oversold detection, EMA crossover alerts, price drop alerts, volume spikes
- Alert levels: `WAIT` → `DIP ALERT` → `BUY ZONE` → `BUY SIGNAL` → `STRONG BUY`
- Overbought/caution warnings when it's time to consider selling
- Alert history log

**Config** (`alerts_config.json`, auto-created on first run):

| Setting | Default | Description |
|---------|---------|-------------|
| `watchlist` | XRP (600), SOL (3) | Coins and holdings to monitor |
| `strategy.rsi_oversold` | `30` | RSI level for strong buy alert |
| `strategy.rsi_buy_zone` | `35` | RSI level for buy zone alert |
| `poll_interval_seconds` | `60` | How often to check prices |

## Built with

- Python 3.13 on Raspberry Pi 5
- [rich](https://github.com/Textualize/rich) for terminal UI
- [ccxt](https://github.com/ccxt/ccxt) for exchange API
- [ta](https://github.com/bukosabino/ta) for technical indicators
- Claude Code
