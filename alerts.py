#!/usr/bin/env python3
"""alerts.py -- Crypto buy indicator dashboard for Raspberry Pi"""

import collections
import datetime
import json
import pathlib
import signal
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
CONFIG_PATH = BASE_DIR / "alerts_config.json"
SPARKLINE_WIDTH = 40
SPARKLINE_BLOCKS = " ▁▂▃▄▅▆▇█"

DEFAULT_CONFIG = {
    "exchange": "coinbase",
    "poll_interval_seconds": 60,
    "watchlist": [
        {"pair": "XRP/USD", "holdings": 600},
        {"pair": "SOL/USD", "holdings": 3},
    ],
    "strategy": {
        "ema_fast": 9,
        "ema_slow": 21,
        "rsi_period": 14,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "rsi_buy_zone": 35,
        "volume_sma_period": 20,
    },
}


# ── Configuration ────────────────────────────────────────────────────────────


class Config:
    def __init__(self):
        if not CONFIG_PATH.exists():
            CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        with open(CONFIG_PATH) as f:
            self._data = json.load(f)

    @property
    def exchange_id(self):
        return self._data["exchange"]

    @property
    def poll_interval(self):
        return self._data["poll_interval_seconds"]

    @property
    def watchlist(self):
        return self._data["watchlist"]

    @property
    def ema_fast(self):
        return self._data["strategy"]["ema_fast"]

    @property
    def ema_slow(self):
        return self._data["strategy"]["ema_slow"]

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
    def rsi_buy_zone(self):
        return self._data["strategy"]["rsi_buy_zone"]

    @property
    def volume_sma_period(self):
        return self._data["strategy"]["volume_sma_period"]


# ── Exchange Client ──────────────────────────────────────────────────────────


class ExchangeClient:
    def __init__(self, exchange_id):
        exchange_class = getattr(ccxt, exchange_id)
        self.exchange = exchange_class({"enableRateLimit": True})

    def fetch_ticker(self, pair):
        return self.exchange.fetch_ticker(pair)

    def fetch_ohlcv(self, pair, timeframe="5m", limit=200):
        return self.exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=limit)


# ── Alert Analyzer ───────────────────────────────────────────────────────────


class AlertAnalyzer:
    def __init__(self, config):
        self.ema_fast = config.ema_fast
        self.ema_slow = config.ema_slow
        self.rsi_period = config.rsi_period
        self.rsi_buy_zone = config.rsi_buy_zone
        self.rsi_oversold = config.rsi_oversold
        self.rsi_overbought = config.rsi_overbought
        self.vol_period = config.volume_sma_period

    def analyze(self, pair, df, holdings, current_price, df_1h=None):
        df = df.copy()
        df["ema_fast"] = ta_lib.trend.ema_indicator(df["close"], window=self.ema_fast)
        df["ema_slow"] = ta_lib.trend.ema_indicator(df["close"], window=self.ema_slow)
        df["rsi"] = ta_lib.momentum.rsi(df["close"], window=self.rsi_period)
        df["vol_sma"] = df["volume"].rolling(window=self.vol_period).mean()
        df["vol_ratio"] = df["volume"] / df["vol_sma"]

        # Bollinger Bands (20-period, 2 std dev)
        bb = ta_lib.volatility.BollingerBands(df["close"], window=20, window_dev=2)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["bb_mid"] = bb.bollinger_mavg()
        df["bb_pct"] = bb.bollinger_pband()  # 0 = at lower, 1 = at upper

        # MACD
        macd_ind = ta_lib.trend.MACD(df["close"], window_slow=26, window_fast=12, window_sign=9)
        df["macd"] = macd_ind.macd()
        df["macd_signal"] = macd_ind.macd_signal()
        df["macd_hist"] = macd_ind.macd_diff()

        curr = df.iloc[-1]
        prev = df.iloc[-2]
        rsi = curr["rsi"]
        ema_f = curr["ema_fast"]
        ema_s = curr["ema_slow"]
        vol_ratio = curr["vol_ratio"] if not pd.isna(curr["vol_ratio"]) else 0.0

        # Bollinger values
        bb_lower = curr["bb_lower"] if not pd.isna(curr["bb_lower"]) else 0
        bb_upper = curr["bb_upper"] if not pd.isna(curr["bb_upper"]) else 0
        bb_mid = curr["bb_mid"] if not pd.isna(curr["bb_mid"]) else 0
        bb_pct = curr["bb_pct"] if not pd.isna(curr["bb_pct"]) else 0.5

        # MACD values
        macd_val = curr["macd"] if not pd.isna(curr["macd"]) else 0
        macd_sig = curr["macd_signal"] if not pd.isna(curr["macd_signal"]) else 0
        macd_hist = curr["macd_hist"] if not pd.isna(curr["macd_hist"]) else 0
        macd_hist_prev = prev["macd_hist"] if not pd.isna(prev["macd_hist"]) else 0

        # MACD bullish divergence: histogram turning up from negative
        macd_turning_up = macd_hist > macd_hist_prev and macd_hist < 0
        macd_cross_up = macd_val > macd_sig and prev["macd"] <= prev["macd_signal"]

        # 1h RSI (bigger picture)
        rsi_1h = None
        if df_1h is not None and len(df_1h) >= 20:
            df_1h = df_1h.copy()
            df_1h["rsi"] = ta_lib.momentum.rsi(df_1h["close"], window=self.rsi_period)
            rsi_1h = df_1h.iloc[-1]["rsi"]
            if pd.isna(rsi_1h):
                rsi_1h = None

        # Support levels: find price levels where bounces happened
        lows = df["low"].tail(100)
        supports = []
        for i in range(2, len(lows) - 2):
            if lows.iloc[i] <= lows.iloc[i-1] and lows.iloc[i] <= lows.iloc[i-2] and \
               lows.iloc[i] <= lows.iloc[i+1] and lows.iloc[i] <= lows.iloc[i+2]:
                supports.append(lows.iloc[i])
        # Keep unique-ish supports (within 0.5% are the same level)
        unique_supports = []
        for s in sorted(supports):
            if not unique_supports or abs(s - unique_supports[-1]) / unique_supports[-1] > 0.005:
                unique_supports.append(s)
        # Only supports below current price
        support_levels = [s for s in unique_supports if s < current_price]
        support_levels = support_levels[-3:]  # Keep top 3 nearest

        recent_high = df["high"].tail(50).max()
        drop_pct = ((current_price - recent_high) / recent_high) * 100

        cross_up = (prev["ema_fast"] <= prev["ema_slow"]) and (ema_f > ema_s)
        cross_down = (prev["ema_fast"] >= prev["ema_slow"]) and (ema_f < ema_s)

        alert = "WAIT"
        reasons = []
        strength = 0

        # Strong buy: RSI oversold
        if rsi <= self.rsi_oversold:
            alert = "STRONG BUY"
            reasons.append(f"RSI {rsi:.0f} - oversold!")
            strength = 3
        elif rsi <= self.rsi_buy_zone:
            alert = "BUY ZONE"
            reasons.append(f"RSI {rsi:.0f} - approaching oversold")
            strength = 2
        elif cross_up:
            alert = "BUY SIGNAL"
            reasons.append("EMA golden cross - momentum turning up")
            strength = 2

        # Sell warning
        if rsi >= self.rsi_overbought:
            if alert == "WAIT":
                alert = "OVERBOUGHT"
            reasons.append(f"RSI {rsi:.0f} - consider taking profit")
            strength = max(strength, 2)

        if cross_down and alert == "WAIT":
            alert = "CAUTION"
            reasons.append("EMA death cross - momentum turning down")
            strength = max(strength, 1)

        # Big dip
        if drop_pct <= -5:
            if alert == "WAIT":
                alert = "DIP ALERT"
            reasons.append(f"{drop_pct:.1f}% from recent high")
            strength = max(strength, 2)
        elif drop_pct <= -3:
            reasons.append(f"{drop_pct:.1f}% pullback")
            strength = max(strength, 1)

        # Volume spike
        if vol_ratio >= 2.0:
            reasons.append(f"Volume {vol_ratio:.1f}x avg - unusual activity")
            strength = max(strength, 1)

        if not reasons:
            trend = "bullish" if ema_f > ema_s else "bearish"
            reasons.append(f"Trend: {trend} | RSI {rsi:.0f} | No dip yet")

        value = holdings * current_price

        # Buy target prices
        buy_support = ema_s
        buy_low = df["low"].tail(50).min()
        buy_dip = recent_high * 0.95
        buy_price = buy_support if buy_support > buy_low else buy_low
        buy_discount = ((buy_price - current_price) / current_price) * 100

        # Bottom score (0-10): how close are we to a bottom?
        bottom_score = 0
        bottom_signals = []
        if rsi <= 30:
            bottom_score += 3
            bottom_signals.append("RSI oversold")
        elif rsi <= 40:
            bottom_score += 1
            bottom_signals.append("RSI getting low")
        if bb_pct <= 0.05:
            bottom_score += 3
            bottom_signals.append("At Bollinger lower band")
        elif bb_pct <= 0.2:
            bottom_score += 1
            bottom_signals.append("Near Bollinger lower band")
        if macd_turning_up:
            bottom_score += 2
            bottom_signals.append("MACD momentum turning up")
        if macd_cross_up:
            bottom_score += 2
            bottom_signals.append("MACD bullish crossover")
        if rsi_1h is not None and rsi_1h <= 35:
            bottom_score += 2
            bottom_signals.append(f"1h RSI {rsi_1h:.0f} oversold")
        if vol_ratio >= 2.0 and rsi < 40:
            bottom_score += 1
            bottom_signals.append("High volume at low RSI (capitulation?)")
        if not bottom_signals:
            bottom_signals.append("No bottom signals yet")
        bottom_score = min(bottom_score, 10)

        return {
            "pair": pair,
            "coin": pair.replace("/USD", ""),
            "price": current_price,
            "holdings": holdings,
            "value": value,
            "rsi": rsi,
            "rsi_1h": rsi_1h,
            "ema_fast": ema_f,
            "ema_slow": ema_s,
            "volume_ratio": vol_ratio,
            "drop_pct": drop_pct,
            "alert": alert,
            "reasons": reasons,
            "strength": strength,
            "buy_price": buy_price,
            "buy_support": buy_support,
            "buy_low": buy_low,
            "buy_dip": buy_dip,
            "buy_discount": buy_discount,
            "bb_lower": bb_lower,
            "bb_upper": bb_upper,
            "bb_pct": bb_pct,
            "macd": macd_val,
            "macd_signal": macd_sig,
            "macd_hist": macd_hist,
            "macd_turning_up": macd_turning_up,
            "support_levels": support_levels,
            "bottom_score": bottom_score,
            "bottom_signals": bottom_signals,
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


def alert_style(alert):
    styles = {
        "STRONG BUY": "bold bright_green",
        "BUY ZONE": "bold green",
        "BUY SIGNAL": "bold cyan",
        "DIP ALERT": "bold yellow",
        "OVERBOUGHT": "bold red",
        "CAUTION": "bold bright_red",
        "WAIT": "dim",
    }
    return styles.get(alert, "dim")


def alert_border(alert):
    if alert in ("STRONG BUY", "BUY ZONE", "BUY SIGNAL"):
        return "green"
    if alert in ("OVERBOUGHT", "CAUTION"):
        return "red"
    if alert == "DIP ALERT":
        return "yellow"
    return "cyan"


# ── Panel Renderers ──────────────────────────────────────────────────────────


def render_header(total_value):
    now = datetime.datetime.now()
    text = Text()
    text.append("  BUY INDICATOR", style="bold bright_white")
    text.append("  |  ", style="dim")
    text.append("Watchlist Alerts", style="bold cyan")
    text.append("  |  ", style="dim")
    text.append(f"Portfolio: {format_usd(total_value)}", style="bold green")
    text.append("  |  ", style="dim")
    text.append(now.strftime("%H:%M:%S"), style="yellow")
    return Panel(Align.center(text), border_style="bright_blue", style="on dark_blue")


def render_coin_panel(data, price_history):
    """Render a full panel for one watchlist coin."""
    coin = data["coin"]
    price = data["price"]
    rsi = data["rsi"]
    alert = data["alert"]
    drop = data["drop_pct"]
    value = data["value"]
    holdings = data["holdings"]
    ema_f = data["ema_fast"]
    ema_s = data["ema_slow"]
    vol = data["volume_ratio"]

    content = Text()

    # Price + change
    content.append("  Price: ", style="dim")
    content.append(format_usd(price), style="bold bright_white")
    if drop < -1:
        content.append(f"  {drop:+.1f}% from high", style="bold red")
    elif drop > -0.5:
        content.append(f"  near high", style="bold green")
    else:
        content.append(f"  {drop:+.1f}%", style="dim")
    content.append("\n")

    # Sparkline
    if price_history and len(price_history) > 1:
        content.append("  ")
        content.append_text(make_sparkline(price_history, "bright_cyan"))
        content.append("\n")
    content.append("\n")

    # Holdings
    content.append("  Holdings: ", style="dim")
    content.append(f"{format_crypto(holdings)} {coin}", style="bright_white")
    content.append(f" = {format_usd(value)}\n", style="bold bright_white")
    content.append("\n")

    # EMAs
    content.append("  EMA(9): ", style="dim")
    content.append(format_usd(ema_f), style="cyan")
    content.append("   EMA(21): ", style="dim")
    content.append(format_usd(ema_s), style="cyan")
    trend = "bullish" if ema_f > ema_s else "bearish"
    t_color = "green" if trend == "bullish" else "red"
    content.append(f"   [{trend}]", style=f"bold {t_color}")
    content.append("\n")

    # RSI bar
    rsi_color = "green" if rsi < 30 else "red" if rsi > 70 else "yellow"
    bar_w = 20
    rsi_fill = min(int((rsi / 100) * bar_w), bar_w)
    content.append("  RSI:    ", style="dim")
    content.append("\u2588" * rsi_fill, style=rsi_color)
    content.append("\u2591" * (bar_w - rsi_fill), style="dim")
    content.append(f" {rsi:.0f}", style=f"bold {rsi_color}")
    if rsi <= 30:
        content.append("  OVERSOLD", style="bold green")
    elif rsi >= 70:
        content.append("  OVERBOUGHT", style="bold red")
    content.append("\n")

    # Volume
    content.append("  Volume: ", style="dim")
    content.append(f"{vol:.1f}x avg", style="bright_white")
    if vol >= 2.0:
        content.append("  HIGH", style="bold yellow")
    content.append("\n\n")

    # Buy targets
    buy_price = data["buy_price"]
    buy_discount = data["buy_discount"]
    content.append("  Buy at:  ", style="dim")
    content.append(format_usd(buy_price), style="bold bright_green")
    content.append(f"  ({buy_discount:+.1f}%)", style="green" if buy_discount < 0 else "red")
    content.append("\n")
    content.append("    EMA support: ", style="dim")
    content.append(f"{format_usd(data['buy_support'])}", style="dim")
    content.append("  |  Recent low: ", style="dim")
    content.append(f"{format_usd(data['buy_low'])}", style="dim")
    content.append("\n\n")

    # ── Bottom Score ──
    bscore = data["bottom_score"]
    bs_bar_w = 10
    bs_fill = min(int((bscore / 10) * bs_bar_w), bs_bar_w)
    if bscore >= 7:
        bs_color = "bright_green"
        bs_label = "LIKELY BOTTOM"
    elif bscore >= 4:
        bs_color = "yellow"
        bs_label = "GETTING CLOSE"
    else:
        bs_color = "dim"
        bs_label = "NOT YET"
    content.append("  Bottom:  ", style="dim")
    content.append("█" * bs_fill, style=bs_color)
    content.append("░" * (bs_bar_w - bs_fill), style="dim")
    content.append(f" {bscore}/10 ", style=f"bold {bs_color}")
    content.append(bs_label, style=f"bold {bs_color}")
    content.append("\n")
    for sig in data["bottom_signals"]:
        content.append(f"    · {sig}\n", style=bs_color)
    content.append("\n")

    # ── Bollinger Bands ──
    bb_pct = data["bb_pct"]
    bb_bar_w = 20
    bb_pos = min(int(bb_pct * bb_bar_w), bb_bar_w - 1) if bb_pct >= 0 else 0
    content.append("  BB:     ", style="dim")
    bb_bar = list("─" * bb_bar_w)
    bb_bar[0] = "▏"
    bb_bar[-1] = "▕"
    if bb_bar_w > 1:
        mid = bb_bar_w // 2
        bb_bar[mid] = "┊"
    bb_bar[bb_pos] = "●"
    for idx, ch in enumerate(bb_bar):
        if ch == "●":
            if bb_pct <= 0.2:
                content.append(ch, style="bold bright_green")
            elif bb_pct >= 0.8:
                content.append(ch, style="bold red")
            else:
                content.append(ch, style="bold yellow")
        else:
            content.append(ch, style="dim")
    content.append("  ", style="dim")
    content.append(format_usd(data["bb_lower"]), style="green")
    content.append(" / ", style="dim")
    content.append(format_usd(data["bb_upper"]), style="red")
    content.append("\n")

    # ── MACD ──
    content.append("  MACD:   ", style="dim")
    macd_h = data["macd_hist"]
    if macd_h > 0:
        content.append(f"▲ {macd_h:.4f}", style="green")
    else:
        content.append(f"▼ {macd_h:.4f}", style="red")
    if data["macd_turning_up"]:
        content.append("  ↑ turning up", style="bold bright_green")
    content.append("\n")

    # ── 1h RSI ──
    rsi_1h = data.get("rsi_1h")
    if rsi_1h is not None:
        rsi_1h_color = "green" if rsi_1h < 35 else "red" if rsi_1h > 65 else "yellow"
        content.append("  1h RSI: ", style="dim")
        content.append(f"{rsi_1h:.0f}", style=f"bold {rsi_1h_color}")
        if rsi_1h <= 35:
            content.append("  oversold on higher TF", style="bold green")
        elif rsi_1h >= 65:
            content.append("  overbought on higher TF", style="bold red")
        content.append("\n")

    # ── Support levels ──
    supports = data["support_levels"]
    if supports:
        content.append("  Support: ", style="dim")
        for j, s in enumerate(supports):
            pct_away = ((s - price) / price) * 100
            content.append(format_usd(s), style="cyan")
            content.append(f" ({pct_away:+.1f}%)", style="dim")
            if j < len(supports) - 1:
                content.append("  ", style="dim")
        content.append("\n")

    content.append("\n")

    # Alert box
    content.append("  >> ", style="bright_white")
    content.append(alert, style=alert_style(alert))
    content.append("\n")
    for r in data["reasons"]:
        content.append(f"     {r}\n", style="bright_white")

    border = alert_border(alert)
    return Panel(content, title=f"[bold]{coin}[/]", border_style=border)


def render_alert_history(alert_log):
    """Render recent alert history."""
    table = Table(expand=True, show_header=True, header_style="bold")
    table.add_column("Time", no_wrap=True, width=8)
    table.add_column("Coin", no_wrap=True, width=5)
    table.add_column("Alert", no_wrap=True)
    table.add_column("Detail", no_wrap=True)

    for entry in alert_log:
        time_str = entry["time"]
        table.add_row(
            time_str,
            Text(entry["coin"], style="bold"),
            Text(entry["alert"], style=alert_style(entry["alert"])),
            entry["reason"],
        )

    return Panel(table, title=f"[bold]Alert History ({len(alert_log)})[/]", border_style="dim")


def render_log(messages, poll_interval, elapsed):
    remaining = max(0, poll_interval - elapsed)
    content = Text()
    recent = list(messages)[-2:] if messages else []
    for msg in recent:
        content.append(f"  {msg}\n", style="dim")
    content.append(f"  Next poll: {remaining:.0f}s", style="bright_cyan")
    return Panel(content, title="[bold]Log[/]", border_style="dim")


# ── Layout ───────────────────────────────────────────────────────────────────


def build_layout(num_coins):
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="coins", ratio=1),
        Layout(name="history", size=8),
        Layout(name="footer", size=5),
    )
    if num_coins == 2:
        layout["coins"].split_row(
            Layout(name="coin_0", ratio=1),
            Layout(name="coin_1", ratio=1),
        )
    elif num_coins == 1:
        layout["coins"].split_row(
            Layout(name="coin_0", ratio=1),
        )
    else:
        # 3+ coins: stack them
        coin_layouts = []
        for i in range(num_coins):
            coin_layouts.append(Layout(name=f"coin_{i}", ratio=1))
        layout["coins"].split_row(*coin_layouts)
    return layout


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    console = Console()

    config = Config()
    exchange = ExchangeClient(config.exchange_id)
    analyzer = AlertAnalyzer(config)

    num_coins = len(config.watchlist)
    layout = build_layout(num_coins)

    log_messages = collections.deque(maxlen=50)
    price_histories = {w["pair"]: collections.deque(maxlen=SPARKLINE_WIDTH) for w in config.watchlist}
    alert_log = collections.deque(maxlen=20)
    coin_data = {}
    last_poll = 0.0
    last_alerts = {}

    coins_str = ", ".join(w["pair"].replace("/USD", "") for w in config.watchlist)
    log_messages.append(f"Watching: {coins_str}")

    with Live(
        layout, console=console, refresh_per_second=2,
        screen=True, vertical_overflow="crop",
    ):
        while True:
            now = time.monotonic()

            if (now - last_poll) >= config.poll_interval or last_poll == 0:
                last_poll = now

                for w in config.watchlist:
                    pair = w["pair"]
                    holdings = w["holdings"]
                    coin = pair.replace("/USD", "")
                    try:
                        ticker = exchange.fetch_ticker(pair)
                        price = ticker["last"]
                        price_histories[pair].append(price)

                        raw = exchange.fetch_ohlcv(pair, "5m", limit=200)
                        df = pd.DataFrame(
                            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
                        )

                        # Fetch 1h candles for multi-timeframe RSI
                        df_1h = None
                        try:
                            raw_1h = exchange.fetch_ohlcv(pair, "1h", limit=100)
                            df_1h = pd.DataFrame(
                                raw_1h, columns=["timestamp", "open", "high", "low", "close", "volume"]
                            )
                            if len(df_1h) < 20:
                                df_1h = None
                        except Exception:
                            pass

                        min_rows = config.ema_slow + 5
                        if len(df) >= min_rows:
                            data = analyzer.analyze(pair, df, holdings, price, df_1h=df_1h)
                            coin_data[pair] = data
                            log_messages.append(
                                f"{coin}: {format_usd(price)} | RSI {data['rsi']:.0f} | {data['alert']}"
                            )

                            # Log new alerts (not WAIT, and different from last)
                            if data["alert"] != "WAIT" and data["alert"] != last_alerts.get(pair):
                                alert_log.appendleft({
                                    "time": datetime.datetime.now().strftime("%H:%M:%S"),
                                    "coin": coin,
                                    "alert": data["alert"],
                                    "reason": data["reasons"][0] if data["reasons"] else "",
                                })
                            last_alerts[pair] = data["alert"]
                        else:
                            log_messages.append(f"{coin}: need more candles ({len(df)}/{min_rows})")

                    except Exception as e:
                        log_messages.append(f"{coin} error: {type(e).__name__}: {e}")

            # ── Render ──
            elapsed = now - last_poll
            total_value = sum(d["value"] for d in coin_data.values())

            layout["header"].update(render_header(total_value))

            for i, w in enumerate(config.watchlist):
                pair = w["pair"]
                slot = layout["coins"][f"coin_{i}"]
                if pair in coin_data:
                    slot.update(
                        render_coin_panel(coin_data[pair], price_histories[pair])
                    )
                else:
                    slot.update(
                        Panel(Text("  Loading...", style="dim"),
                              title=f"[bold]{pair.replace('/USD', '')}[/]",
                              border_style="dim")
                    )

            layout["history"].update(render_alert_history(list(alert_log)[:8]))
            layout["footer"].update(render_log(log_messages, config.poll_interval, elapsed))

            time.sleep(0.5)


if __name__ == "__main__":
    main()
