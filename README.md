# Network

Raspberry Pi terminal tools — a live network dashboard and a crypto trading bot.

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

**Requirements:** `psutil`, `rich` (+ system tools: `iwconfig`, `ip`, `arp`)

## trader.py

Crypto trading bot with a live TUI dashboard. Day trades DOGE/USD and monitors XRP + SOL for buy-the-dip alerts.

```
python3 trader.py
```

**Features:**
- **DOGE day trader** — EMA crossover + RSI + volume strategy, paper trading by default
- **XRP/SOL buy alerts** — watches for oversold RSI, price dips, and golden crosses
- **Risk management** — stop-loss, take-profit, daily loss limit, position sizing
- **SQLite persistence** — trades and portfolio survive restarts
- **Live dashboard** — sparklines, signal confidence bars, P&L tracking

**Requirements:** `ccxt`, `pandas`, `ta`, `rich`

### Setup

```bash
pip3 install ccxt pandas ta rich psutil
python3 trader.py    # auto-creates config.json on first run
```

Edit `config.json` to set your trading pair, strategy parameters, and API keys. Paper trading is on by default — set `"live_trading": true` to use real money.

### Config

| Setting | Default | Description |
|---------|---------|-------------|
| `trading.pair` | `DOGE/USD` | Coin the bot actively trades |
| `trading.live_trading` | `false` | Paper mode by default |
| `strategy.ema_fast_period` | `9` | Fast EMA window |
| `strategy.ema_slow_period` | `21` | Slow EMA window |
| `risk.stop_loss_pct` | `0.04` | 4% stop-loss |
| `risk.take_profit_pct` | `0.08` | 8% take-profit |
| `risk.daily_loss_limit_usd` | `10.0` | Stop trading after $10 loss/day |
| `watchlist` | XRP, SOL | Coins to monitor for buy alerts |

## Built with

- Python 3.13 on Raspberry Pi 5
- [rich](https://github.com/Textualize/rich) for terminal UI
- [ccxt](https://github.com/ccxt/ccxt) for exchange API
- [ta](https://github.com/bukosabino/ta) for technical indicators
- Claude Code
