#!/usr/bin/env python3
"""trader.py -- DOGE day trading bot with live TUI dashboard for Raspberry Pi"""

import collections
import datetime
import json
import pathlib
import signal
import sqlite3
import sys
import time

import ccxt
import pandas as pd
import ta as ta_lib
from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ── Constants ────────────────────────────────────────────────────────────────

BASE_DIR = pathlib.Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "trades.db"
SPARKLINE_WIDTH = 40
SPARKLINE_BLOCKS = " ▁▂▃▄▅▆▇█"
VERSION = "2.0.0"

DEFAULT_CONFIG = {
    "exchange": {
        "id": "coinbase",
        "api_key": "",
        "api_secret": "",
    },
    "trading": {
        "pair": "DOGE/USD",
        "timeframe": "5m",
        "poll_interval_seconds": 60,
        "live_trading": False,
    },
    "strategy": {
        "ema_fast_period": 9,
        "ema_slow_period": 21,
        "rsi_period": 14,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "volume_sma_period": 20,
        "volume_threshold": 1.0,
    },
    "risk": {
        "max_position_pct": 0.90,
        "stop_loss_pct": 0.04,
        "take_profit_pct": 0.08,
        "daily_loss_limit_usd": 10.0,
        "min_trade_usd": 5.0,
        "max_trade_usd": 50.0,
    },
    "paper": {
        "initial_doge": 500.0,
    },
}


# ── Configuration ────────────────────────────────────────────────────────────


class Config:
    def __init__(self):
        if not CONFIG_PATH.exists():
            CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        with open(CONFIG_PATH) as f:
            self._data = json.load(f)
        self._validate()

    def _validate(self):
        if self.live_trading and not self.api_key:
            raise SystemExit(
                "ERROR: live_trading is true but no API key in config.json"
            )

    @property
    def exchange_id(self):
        return self._data["exchange"]["id"]

    @property
    def api_key(self):
        return self._data["exchange"]["api_key"]

    @property
    def api_secret(self):
        return self._data["exchange"]["api_secret"]

    @property
    def pair(self):
        return self._data["trading"]["pair"]

    @property
    def coin_name(self):
        return self.pair.replace("/USD", "")

    @property
    def timeframe(self):
        return self._data["trading"]["timeframe"]

    @property
    def poll_interval(self):
        return self._data["trading"]["poll_interval_seconds"]

    @property
    def live_trading(self):
        return self._data["trading"]["live_trading"]

    @property
    def ema_fast(self):
        return self._data["strategy"]["ema_fast_period"]

    @property
    def ema_slow(self):
        return self._data["strategy"]["ema_slow_period"]

    @property
    def rsi_period(self):
        return self._data["strategy"]["rsi_period"]

    @property
    def rsi_overbought(self):
        return self._data["strategy"]["rsi_overbought"]

    @property
    def rsi_oversold(self):
        return self._data["strategy"]["rsi_oversold"]

    @property
    def volume_sma_period(self):
        return self._data["strategy"]["volume_sma_period"]

    @property
    def volume_threshold(self):
        return self._data["strategy"]["volume_threshold"]

    @property
    def max_position_pct(self):
        return self._data["risk"]["max_position_pct"]

    @property
    def stop_loss_pct(self):
        return self._data["risk"]["stop_loss_pct"]

    @property
    def take_profit_pct(self):
        return self._data["risk"]["take_profit_pct"]

    @property
    def daily_loss_limit(self):
        return self._data["risk"]["daily_loss_limit_usd"]

    @property
    def min_trade(self):
        return self._data["risk"]["min_trade_usd"]

    @property
    def max_trade(self):
        return self._data["risk"]["max_trade_usd"]

    @property
    def initial_crypto(self):
        return self._data["paper"].get("initial_doge", 500.0)


# ── Database ─────────────────────────────────────────────────────────────────


class Database:
    def __init__(self, path=DB_PATH):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                UNIQUE(pair, timeframe, timestamp)
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                pair TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                amount REAL NOT NULL,
                cost REAL NOT NULL,
                fee REAL NOT NULL DEFAULT 0,
                mode TEXT NOT NULL,
                order_id TEXT,
                signal TEXT,
                notes TEXT
            );
            CREATE TABLE IF NOT EXISTS portfolio (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                usd_balance REAL NOT NULL,
                crypto_balance REAL NOT NULL DEFAULT 0,
                avg_entry_price REAL NOT NULL DEFAULT 0,
                mode TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                starting_value REAL NOT NULL,
                realized_pnl REAL NOT NULL DEFAULT 0,
                trades_count INTEGER NOT NULL DEFAULT 0
            );
        """
        )
        self.conn.commit()

    def save_candles(self, pair, timeframe, candles):
        self.conn.executemany(
            """INSERT OR REPLACE INTO candles
               (pair, timeframe, timestamp, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(pair, timeframe, c[0], c[1], c[2], c[3], c[4], c[5]) for c in candles],
        )
        self.conn.commit()

    def log_trade(self, pair, side, price, amount, cost, fee, mode, order_id, sig, notes=""):
        self.conn.execute(
            """INSERT INTO trades
               (timestamp, pair, side, price, amount, cost, fee, mode, order_id, signal, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.datetime.now().isoformat(),
                pair, side, price, amount, cost, fee, mode, order_id, sig, notes,
            ),
        )
        self.conn.commit()

    def get_recent_trades(self, limit=10):
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def save_portfolio(self, usd, crypto, avg_price, mode):
        self.conn.execute(
            """INSERT INTO portfolio (id, usd_balance, crypto_balance, avg_entry_price, mode, updated_at)
               VALUES (1, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   usd_balance=excluded.usd_balance,
                   crypto_balance=excluded.crypto_balance,
                   avg_entry_price=excluded.avg_entry_price,
                   mode=excluded.mode,
                   updated_at=excluded.updated_at""",
            (usd, crypto, avg_price, mode, datetime.datetime.now().isoformat()),
        )
        self.conn.commit()

    def load_portfolio(self):
        row = self.conn.execute("SELECT * FROM portfolio WHERE id=1").fetchone()
        return dict(row) if row else None

    def get_daily_stats(self, date_str):
        row = self.conn.execute(
            "SELECT * FROM daily_stats WHERE date=?", (date_str,)
        ).fetchone()
        return dict(row) if row else None

    def update_daily_pnl(self, date_str, pnl_delta, starting_value=0):
        existing = self.get_daily_stats(date_str)
        if existing:
            self.conn.execute(
                """UPDATE daily_stats SET realized_pnl = realized_pnl + ?,
                   trades_count = trades_count + 1 WHERE date = ?""",
                (pnl_delta, date_str),
            )
        else:
            self.conn.execute(
                """INSERT INTO daily_stats (date, starting_value, realized_pnl, trades_count)
                   VALUES (?, ?, ?, 1)""",
                (date_str, starting_value, pnl_delta),
            )
        self.conn.commit()


# ── Exchange Client ──────────────────────────────────────────────────────────


class ExchangeClient:
    def __init__(self, config):
        exchange_class = getattr(ccxt, config.exchange_id)
        params = {"enableRateLimit": True}
        if config.api_key:
            params["apiKey"] = config.api_key
            params["secret"] = config.api_secret
        self.exchange = exchange_class(params)

    def fetch_ticker(self, pair):
        return self.exchange.fetch_ticker(pair)

    def fetch_ohlcv(self, pair, timeframe="5m", limit=200):
        return self.exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=limit)

    def place_order(self, pair, side, amount):
        return self.exchange.create_market_order(pair, side, amount)


# ── Strategy ─────────────────────────────────────────────────────────────────

BUY = "BUY"
SELL = "SELL"
HOLD = "HOLD"


class Strategy:
    def __init__(self, config):
        self.ema_fast_period = config.ema_fast
        self.ema_slow_period = config.ema_slow
        self.rsi_period = config.rsi_period
        self.rsi_ob = config.rsi_overbought
        self.rsi_os = config.rsi_oversold
        self.vol_period = config.volume_sma_period
        self.vol_thresh = config.volume_threshold

    def analyze(self, df):
        df = df.copy()
        df["ema_fast"] = ta_lib.trend.ema_indicator(df["close"], window=self.ema_fast_period)
        df["ema_slow"] = ta_lib.trend.ema_indicator(df["close"], window=self.ema_slow_period)
        df["rsi"] = ta_lib.momentum.rsi(df["close"], window=self.rsi_period)
        df["vol_sma"] = df["volume"].rolling(window=self.vol_period).mean()
        df["vol_ratio"] = df["volume"] / df["vol_sma"]

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        ema_f = curr["ema_fast"]
        ema_s = curr["ema_slow"]
        ema_f_prev = prev["ema_fast"]
        ema_s_prev = prev["ema_slow"]
        rsi = curr["rsi"]
        vol_ratio = curr["vol_ratio"] if not pd.isna(curr["vol_ratio"]) else 0.0

        reasons = []
        sig = HOLD
        confidence = 0.0

        cross_up = (ema_f_prev <= ema_s_prev) and (ema_f > ema_s)
        if cross_up:
            reasons.append("EMA golden cross")
            confidence += 0.4
            if rsi < self.rsi_ob:
                reasons.append(f"RSI {rsi:.0f} (not overbought)")
                confidence += 0.2
            if rsi < 45:
                reasons.append(f"RSI {rsi:.0f} (momentum room)")
                confidence += 0.1
            if vol_ratio >= self.vol_thresh:
                reasons.append(f"Volume {vol_ratio:.1f}x (confirmed)")
                confidence += 0.2
            else:
                reasons.append(f"Volume {vol_ratio:.1f}x (weak)")
                confidence -= 0.1
            if confidence >= 0.5:
                sig = BUY

        cross_down = (ema_f_prev >= ema_s_prev) and (ema_f < ema_s)
        if cross_down:
            reasons.append("EMA death cross")
            confidence += 0.4
            if rsi > self.rsi_ob:
                reasons.append(f"RSI {rsi:.0f} (overbought)")
                confidence += 0.3
            if vol_ratio >= self.vol_thresh:
                reasons.append(f"Volume {vol_ratio:.1f}x (selling pressure)")
                confidence += 0.2
            if confidence >= 0.5:
                sig = SELL

        if sig == HOLD and rsi > 80 and ema_f < ema_s:
            sig = SELL
            confidence = 0.6
            reasons.append(f"RSI {rsi:.0f} extreme + bearish trend")

        if not reasons:
            trend = "bullish" if ema_f > ema_s else "bearish"
            reasons.append(f"Trend: {trend} | RSI {rsi:.0f}")

        return {
            "signal": sig,
            "ema_fast": ema_f,
            "ema_slow": ema_s,
            "rsi": rsi,
            "volume_ratio": vol_ratio,
            "reasons": reasons,
            "confidence": min(max(confidence, 0.0), 1.0),
            "close": curr["close"],
        }


# ── Risk Manager ─────────────────────────────────────────────────────────────


class RiskManager:
    def __init__(self, config, db):
        self.max_pos_pct = config.max_position_pct
        self.stop_loss_pct = config.stop_loss_pct
        self.take_profit_pct = config.take_profit_pct
        self.daily_limit = config.daily_loss_limit
        self.min_trade = config.min_trade
        self.max_trade = config.max_trade
        self.db = db

    def check_stop_loss(self, entry_price, current_price):
        if entry_price <= 0:
            return False
        return (current_price - entry_price) / entry_price <= -self.stop_loss_pct

    def check_take_profit(self, entry_price, current_price):
        if entry_price <= 0:
            return False
        return (current_price - entry_price) / entry_price >= self.take_profit_pct

    def check_daily_limit(self):
        today = datetime.date.today().isoformat()
        stats = self.db.get_daily_stats(today)
        if stats and stats["realized_pnl"] <= -self.daily_limit:
            return False
        return True

    def position_size(self, portfolio_value, confidence=1.0):
        max_by_pct = portfolio_value * self.max_pos_pct
        base = min(max_by_pct, self.max_trade)
        sized = base * confidence
        if sized < self.min_trade:
            return 0.0
        return round(sized, 2)

    def validate(self, side, usd_amount, portfolio):
        if not self.check_daily_limit():
            return False, "Daily loss limit reached"
        if side == "buy" and usd_amount > portfolio.usd_balance:
            return False, f"Insufficient USD (${portfolio.usd_balance:.2f})"
        if side == "buy" and usd_amount < self.min_trade:
            return False, f"Below min trade (${self.min_trade:.2f})"
        if side == "sell" and not portfolio.has_position:
            return False, "Nothing to sell"
        return True, "OK"


# ── Portfolio ────────────────────────────────────────────────────────────────


class Portfolio:
    FEE_PCT = 0.006

    def __init__(self, db, config):
        self.db = db
        self.mode = "paper" if not config.live_trading else "live"
        saved = self.db.load_portfolio()
        if saved and saved["mode"] == self.mode:
            self.usd_balance = saved["usd_balance"]
            self.crypto_balance = saved["crypto_balance"]
            self.avg_entry_price = saved["avg_entry_price"]
        else:
            self.usd_balance = 0.0
            self.crypto_balance = config.initial_crypto
            self.avg_entry_price = 0.0

    @property
    def has_position(self):
        return self.crypto_balance > 0.5

    def total_value(self, price):
        return self.usd_balance + (self.crypto_balance * price)

    def unrealized_pnl(self, price):
        if not self.has_position or self.avg_entry_price <= 0:
            return 0.0
        return (price - self.avg_entry_price) * self.crypto_balance

    def unrealized_pnl_pct(self, price):
        if not self.has_position or self.avg_entry_price <= 0:
            return 0.0
        return ((price - self.avg_entry_price) / self.avg_entry_price) * 100

    def buy(self, price, usd_amount):
        fee = usd_amount * self.FEE_PCT
        net = usd_amount - fee
        crypto = net / price
        total_cost = (self.avg_entry_price * self.crypto_balance) + net
        self.crypto_balance += crypto
        self.avg_entry_price = total_cost / self.crypto_balance if self.crypto_balance > 0 else 0
        self.usd_balance -= usd_amount
        self._save()
        return crypto, fee

    def sell(self, price, crypto_amount=None):
        if crypto_amount is None:
            crypto_amount = self.crypto_balance
        gross = crypto_amount * price
        fee = gross * self.FEE_PCT
        net = gross - fee
        pnl = (price - self.avg_entry_price) * crypto_amount if self.avg_entry_price > 0 else 0
        self.crypto_balance -= crypto_amount
        self.usd_balance += net
        if self.crypto_balance < 0.5:
            self.crypto_balance = 0.0
            self.avg_entry_price = 0.0
        self._save()
        return net, fee, pnl

    def _save(self):
        self.db.save_portfolio(
            self.usd_balance, self.crypto_balance, self.avg_entry_price, self.mode
        )


# ── Trade Executor ───────────────────────────────────────────────────────────


class TradeExecutor:
    def __init__(self, config, exchange, portfolio, risk, db):
        self.config = config
        self.exchange = exchange
        self.portfolio = portfolio
        self.risk = risk
        self.db = db
        self.live = config.live_trading

    def execute(self, analysis, price):
        if self.portfolio.has_position:
            if self.risk.check_stop_loss(self.portfolio.avg_entry_price, price):
                return self._sell("stop_loss", price)
            if self.risk.check_take_profit(self.portfolio.avg_entry_price, price):
                return self._sell("take_profit", price)

        sig = analysis["signal"]
        if sig == BUY and not self.portfolio.has_position:
            value = self.portfolio.total_value(price)
            usd = self.risk.position_size(value, analysis["confidence"])
            ok, reason = self.risk.validate("buy", usd, self.portfolio)
            if not ok:
                return {"action": "BLOCKED", "reason": reason}
            return self._buy(usd, price, analysis)
        elif sig == SELL and self.portfolio.has_position:
            return self._sell("signal", price)

        return {"action": "HOLD", "reason": "; ".join(analysis["reasons"])}

    def _buy(self, usd, price, analysis):
        if self.live:
            crypto = usd / price
            order = self.exchange.place_order(self.config.pair, "buy", crypto)
            fp = order.get("average", price)
            fa = order.get("filled", crypto)
            fc = order.get("cost", usd)
            fee = order.get("fee", {}).get("cost", 0)
            self.portfolio.buy(fp, fc)
            oid = order.get("id")
        else:
            fa, fee = self.portfolio.buy(price, usd)
            fp, fc, oid = price, usd, None

        self.db.log_trade(
            self.config.pair, "buy", fp, fa, fc, fee,
            "live" if self.live else "paper", oid, "ema_cross",
            "; ".join(analysis["reasons"]),
        )
        today = datetime.date.today().isoformat()
        self.db.update_daily_pnl(today, 0, self.portfolio.total_value(price))
        return {"action": "BUY", "amount": fa, "price": fp, "cost": fc, "fee": fee}

    def _sell(self, sig_name, price):
        amount = self.portfolio.crypto_balance
        if self.live:
            order = self.exchange.place_order(self.config.pair, "sell", amount)
            fp = order.get("average", price)
            net, fee, pnl = self.portfolio.sell(fp)
            oid = order.get("id")
        else:
            net, fee, pnl = self.portfolio.sell(price)
            fp, oid = price, None

        self.db.log_trade(
            self.config.pair, "sell", fp, amount, net, fee,
            "live" if self.live else "paper", oid, sig_name,
            f"P&L: ${pnl:+.2f}",
        )
        today = datetime.date.today().isoformat()
        self.db.update_daily_pnl(today, pnl, self.portfolio.total_value(price))
        return {
            "action": "SELL", "price": fp, "pnl": pnl,
            "net": net, "fee": fee, "signal": sig_name,
        }


# ── Utilities ────────────────────────────────────────────────────────────────


def format_usd(n):
    if abs(n) >= 1:
        return f"${n:,.2f}"
    return f"${n:.4f}"


def format_crypto(n):
    if n >= 1:
        return f"{n:.2f}"
    return f"{n:.8f}"


def make_sparkline(history, color="green"):
    if not history:
        return Text("")
    max_val = max(history) or 1
    min_val = min(history)
    rng = max_val - min_val or 1
    chars = ""
    for v in history:
        idx = min(int(((v - min_val) / rng) * 8), 8)
        chars += SPARKLINE_BLOCKS[idx]
    return Text(chars, style=color)


def pnl_color(val):
    if val > 0:
        return "bold green"
    elif val < 0:
        return "bold red"
    return "dim"


# ── Panel Renderers ──────────────────────────────────────────────────────────


def render_header(config, portfolio, price):
    mode = "PAPER" if portfolio.mode == "paper" else "LIVE"
    mode_style = "bold yellow" if mode == "PAPER" else "bold red"
    now = datetime.datetime.now()

    text = Text()
    text.append(f"  {config.coin_name} TRADER", style="bold bright_white")
    text.append(f"  v{VERSION}", style="dim")
    text.append("  |  ", style="dim")
    text.append(config.pair, style="bold cyan")
    text.append("  |  ", style="dim")
    text.append(mode, style=mode_style)
    text.append("  |  ", style="dim")
    text.append(now.strftime("%Y-%m-%d %H:%M:%S"), style="yellow")

    return Panel(Align.center(text), border_style="bright_blue", style="on dark_blue")


def render_price(price, analysis, price_history, coin_name):
    content = Text()

    if price > 0:
        content.append(f"  {coin_name}: ", style="dim")
        content.append(format_usd(price), style="bold bright_white")

        if len(price_history) > 1:
            chg = ((price - price_history[0]) / price_history[0]) * 100
            color = "green" if chg >= 0 else "red"
            content.append(f"  {chg:+.2f}%", style=f"bold {color}")
        content.append("\n")

        content.append("  ")
        content.append_text(make_sparkline(price_history, "bright_yellow"))
        content.append("\n\n")
    else:
        content.append("  Waiting for data...\n\n", style="dim")

    if analysis:
        content.append(f"  EMA(9): ", style="dim")
        content.append(format_usd(analysis["ema_fast"]), style="cyan")
        content.append(f"   EMA(21): ", style="dim")
        content.append(f"{format_usd(analysis['ema_slow'])}\n", style="cyan")

        rsi = analysis["rsi"]
        rsi_color = "green" if rsi < 30 else "red" if rsi > 70 else "yellow"
        content.append("  RSI: ", style="dim")
        content.append(f"{rsi:.1f}", style=f"bold {rsi_color}")

        vr = analysis["volume_ratio"]
        content.append("   Vol: ", style="dim")
        content.append(f"{vr:.1f}x avg", style="bright_white")

        trend = "BULLISH" if analysis["ema_fast"] > analysis["ema_slow"] else "BEARISH"
        t_color = "green" if trend == "BULLISH" else "red"
        content.append("   Trend: ", style="dim")
        content.append(trend, style=f"bold {t_color}")
        content.append("\n")

    return Panel(content, title="[bold]Price & Trend[/]", border_style="cyan")


def render_portfolio(portfolio, price, coin_name):
    content = Text()
    content.append("  USD:    ", style="dim")
    content.append(f"{format_usd(portfolio.usd_balance)}\n", style="bright_white")

    content.append(f"  {coin_name}:   ", style="dim")
    content.append(f"{format_crypto(portfolio.crypto_balance)}\n", style="bright_white")

    total = portfolio.total_value(price) if price > 0 else portfolio.usd_balance
    content.append("  Value:  ", style="dim")
    content.append(f"{format_usd(total)}\n", style="bold bright_white")

    if portfolio.has_position and portfolio.avg_entry_price > 0 and price > 0:
        pnl = portfolio.unrealized_pnl(price)
        pnl_pct = portfolio.unrealized_pnl_pct(price)
        content.append("  P&L:    ", style="dim")
        content.append(f"{format_usd(pnl)} ({pnl_pct:+.2f}%)\n", style=pnl_color(pnl))
    else:
        content.append("  P&L:    ", style="dim")
        content.append("--\n", style="dim")

    content.append("\n  Mode:   ", style="dim")
    if portfolio.mode == "paper":
        content.append("PAPER TRADING", style="bold yellow")
    else:
        content.append("LIVE TRADING", style="bold red")
    content.append("\n")

    return Panel(content, title="[bold]Portfolio[/]", border_style="green")


def render_signals(analysis):
    content = Text()
    if not analysis:
        content.append("  Waiting for first analysis...\n", style="dim")
        return Panel(content, title="[bold]Signal[/]", border_style="dim")

    sig = analysis["signal"]
    sig_color = {"BUY": "bold green", "SELL": "bold red", "HOLD": "dim yellow"}
    content.append("  Signal: ", style="dim")
    content.append(sig, style=sig_color.get(sig, "dim"))
    content.append("\n")

    conf = analysis["confidence"]
    bar_width = 15
    filled = int(conf * bar_width)
    content.append("  Confidence: ", style="dim")
    content.append("\u2588" * filled, style="green" if conf >= 0.5 else "yellow")
    content.append("\u2591" * (bar_width - filled), style="dim")
    content.append(f" {conf:.0%}\n\n", style="bright_white")

    content.append("  Reasons:\n", style="dim")
    for r in analysis["reasons"]:
        content.append(f"   - {r}\n", style="bright_white")

    return Panel(content, title="[bold]Signal[/]", border_style="magenta")


def render_position(portfolio, price, config):
    content = Text()
    if not portfolio.has_position:
        content.append("  No position open\n", style="dim")
        content.append(f"  USD ready: {format_usd(portfolio.usd_balance)}\n", style="bright_white")
        content.append("  Waiting for BUY signal...\n", style="dim")
        return Panel(content, title="[bold]Position[/]", border_style="dim")

    entry = portfolio.avg_entry_price
    if entry <= 0:
        content.append(f"  Holding {format_crypto(portfolio.crypto_balance)} {config.coin_name}\n", style="bright_white")
        content.append("  Entry price pending...\n", style="dim")
        return Panel(content, title="[bold]Position[/]", border_style="yellow")

    stop = entry * (1 - config.stop_loss_pct)
    target = entry * (1 + config.take_profit_pct)

    content.append("  Entry:   ", style="dim")
    content.append(f"{format_usd(entry)}\n", style="bright_white")
    content.append("  Stop:    ", style="dim")
    content.append(f"{format_usd(stop)} (-{config.stop_loss_pct*100:.0f}%)\n", style="red")
    content.append("  Target:  ", style="dim")
    content.append(f"{format_usd(target)} (+{config.take_profit_pct*100:.0f}%)\n", style="green")

    total_range = target - stop
    if total_range > 0 and price > 0:
        progress = (price - stop) / total_range
        progress = max(0, min(1, progress))
        bar_w = 20
        filled = int(progress * bar_w)
        content.append("  ")
        content.append("\u2588" * filled, style="green" if progress > 0.5 else "yellow")
        content.append("\u2591" * (bar_w - filled), style="dim")
        pct_to_target = ((target - price) / price) * 100 if price > 0 else 0
        content.append(f" {pct_to_target:.1f}% to target\n", style="dim")

    return Panel(content, title="[bold]Position[/]", border_style="yellow")


def render_trades(recent_trades, coin_name):
    table = Table(expand=True, show_header=True, header_style="bold")
    table.add_column("Time", no_wrap=True, width=8)
    table.add_column("Side", no_wrap=True, width=4)
    table.add_column("Price", no_wrap=True, justify="right")
    table.add_column(coin_name, no_wrap=True, justify="right")
    table.add_column("P&L", no_wrap=True, justify="right")
    table.add_column("Why", no_wrap=True, max_width=12)

    for t in recent_trades:
        ts = t["timestamp"]
        try:
            time_str = datetime.datetime.fromisoformat(ts).strftime("%H:%M:%S")
        except Exception:
            time_str = ts[:8]
        side_style = "bold green" if t["side"] == "buy" else "bold red"
        pnl_str = t["notes"] if t["side"] == "sell" and t["notes"] else ""
        table.add_row(
            time_str,
            Text(t["side"].upper(), style=side_style),
            format_usd(t["price"]),
            format_crypto(t["amount"]),
            pnl_str,
            t["signal"] or "",
        )

    return Panel(table, title=f"[bold]Trades ({len(recent_trades)})[/]", border_style="red")


def render_log(messages, poll_interval, elapsed):
    remaining = max(0, poll_interval - elapsed)
    content = Text()
    recent = list(messages)[-2:] if messages else []
    for msg in recent:
        content.append(f"  {msg}\n", style="dim")
    content.append(f"  Next poll: {remaining:.0f}s", style="bright_cyan")
    return Panel(content, title="[bold]Log[/]", border_style="dim")


# ── Layout ───────────────────────────────────────────────────────────────────


def build_layout():
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="upper", size=11),
        Layout(name="middle", size=13),
        Layout(name="trades", ratio=1),
        Layout(name="footer", size=5),
    )
    layout["upper"].split_row(
        Layout(name="price", ratio=3),
        Layout(name="portfolio", ratio=2),
    )
    layout["middle"].split_row(
        Layout(name="signals", ratio=1),
        Layout(name="position", ratio=1),
    )
    return layout


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    console = Console()

    config = Config()
    if config.live_trading:
        console.print(
            Panel(
                "[bold red]WARNING: LIVE TRADING MODE[/]\n"
                "Real money will be used on Coinbase.\n"
                "Press Ctrl+C within 5 seconds to abort.",
                border_style="red",
            )
        )
        time.sleep(5)

    db = Database()
    exchange = ExchangeClient(config)
    portfolio = Portfolio(db, config)
    risk = RiskManager(config, db)
    strategy = Strategy(config)
    executor = TradeExecutor(config, exchange, portfolio, risk, db)

    layout = build_layout()
    log_messages = collections.deque(maxlen=50)
    price_history = collections.deque(maxlen=SPARKLINE_WIDTH)
    last_analysis = None
    current_price = 0.0
    last_poll = 0.0

    mode_str = "PAPER" if portfolio.mode == "paper" else "LIVE"
    log_messages.append(f"Started in {mode_str} mode")
    log_messages.append(f"Trading {config.pair} on {config.exchange_id}")

    with Live(
        layout, console=console, refresh_per_second=2,
        screen=True, vertical_overflow="crop",
    ):
        while True:
            now = time.monotonic()

            if (now - last_poll) >= config.poll_interval or last_poll == 0:
                last_poll = now
                try:
                    ticker = exchange.fetch_ticker(config.pair)
                    current_price = ticker["last"]
                    price_history.append(current_price)

                    if portfolio.has_position and portfolio.avg_entry_price <= 0:
                        portfolio.avg_entry_price = current_price
                        portfolio._save()
                        log_messages.append(f"Set entry: {format_usd(current_price)}")

                    raw = exchange.fetch_ohlcv(config.pair, config.timeframe, limit=200)
                    db.save_candles(config.pair, config.timeframe, raw)
                    df = pd.DataFrame(
                        raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
                    )

                    min_rows = config.ema_slow + 5
                    if len(df) >= min_rows:
                        last_analysis = strategy.analyze(df)
                        log_messages.append(
                            f"{config.coin_name} {format_usd(current_price)} | "
                            f"{last_analysis['signal']} ({last_analysis['confidence']:.0%})"
                        )
                        result = executor.execute(last_analysis, current_price)
                        if result["action"] not in ("HOLD",):
                            log_messages.append(
                                f">>> {result['action']} at {format_usd(current_price)}"
                            )
                    else:
                        log_messages.append(f"Need {min_rows} candles, got {len(df)}")

                except ccxt.NetworkError as e:
                    log_messages.append(f"Network error: {e}")
                except ccxt.ExchangeError as e:
                    log_messages.append(f"Exchange error: {e}")
                except Exception as e:
                    log_messages.append(f"Error: {type(e).__name__}: {e}")

            # ── Render ──
            elapsed = now - last_poll
            layout["header"].update(render_header(config, portfolio, current_price))
            layout["upper"]["price"].update(
                render_price(current_price, last_analysis, price_history, config.coin_name)
            )
            layout["upper"]["portfolio"].update(
                render_portfolio(portfolio, current_price, config.coin_name)
            )
            layout["middle"]["signals"].update(render_signals(last_analysis))
            layout["middle"]["position"].update(
                render_position(portfolio, current_price, config)
            )
            layout["trades"].update(render_trades(db.get_recent_trades(10), config.coin_name))
            layout["footer"].update(render_log(log_messages, config.poll_interval, elapsed))

            time.sleep(0.5)


if __name__ == "__main__":
    main()
