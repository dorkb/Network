#!/usr/bin/env python3
"""web_alerts.py -- Web dashboard for crypto buy indicators"""

import collections
import copy
import datetime
import json
import signal
import sys
import threading
import time

import numpy as np
import pandas as pd
from flask import Flask, Response

from alerts import Config, ExchangeClient, AlertAnalyzer

# ── App & State ─────────────────────────────────────────────────────────────

app = Flask(__name__)

data_lock = threading.Lock()
coin_data = {}
price_histories = {}
alert_log = collections.deque(maxlen=50)
log_messages = collections.deque(maxlen=50)
last_update_time = ""
poll_countdown = 0


# ── Background Poller ───────────────────────────────────────────────────────


def poll_loop():
    global coin_data, last_update_time, poll_countdown

    config = Config()
    exchange = ExchangeClient(config.exchange_id)
    analyzer = AlertAnalyzer(config)

    for w in config.watchlist:
        price_histories[w["pair"]] = collections.deque(maxlen=40)

    last_alerts = {}
    last_poll = 0.0

    with data_lock:
        coins_str = ", ".join(w["pair"].replace("/USD", "") for w in config.watchlist)
        log_messages.append(f"Watching: {coins_str}")

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

                    with data_lock:
                        price_histories[pair].append(price)

                    raw = exchange.fetch_ohlcv(pair, "5m", limit=200)
                    df = pd.DataFrame(
                        raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
                    )

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

                        with data_lock:
                            coin_data[pair] = data
                            log_messages.append(
                                f"{coin}: ${price:.4f} | RSI {data['rsi']:.0f} | {data['alert']}"
                            )

                            if data["alert"] != "WAIT" and data["alert"] != last_alerts.get(pair):
                                alert_log.appendleft({
                                    "time": datetime.datetime.now().strftime("%H:%M:%S"),
                                    "coin": coin,
                                    "alert": data["alert"],
                                    "reason": data["reasons"][0] if data["reasons"] else "",
                                })
                            last_alerts[pair] = data["alert"]
                    else:
                        with data_lock:
                            log_messages.append(f"{coin}: need more candles ({len(df)}/{min_rows})")

                except Exception as e:
                    with data_lock:
                        log_messages.append(f"{coin} error: {type(e).__name__}: {e}")

            with data_lock:
                last_update_time = datetime.datetime.now().isoformat()

        with data_lock:
            poll_countdown = max(0, config.poll_interval - (time.monotonic() - last_poll))

        time.sleep(1)


# ── Snapshot ────────────────────────────────────────────────────────────────


def get_snapshot():
    with data_lock:
        snapshot = {
            "coins": [],
            "alert_log": list(alert_log)[:10],
            "log_messages": list(log_messages)[-3:],
            "last_update": last_update_time,
            "poll_countdown": int(poll_countdown),
            "total_value": sum(d.get("value", 0) for d in coin_data.values()),
            "timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
        }
        for pair, data in coin_data.items():
            entry = copy.deepcopy(data)
            entry["price_history"] = list(price_histories.get(pair, []))
            # Sanitize numpy/NaN types for JSON
            for key in list(entry.keys()):
                val = entry[key]
                if isinstance(val, (np.bool_,)):
                    entry[key] = bool(val)
                elif isinstance(val, (np.integer,)):
                    entry[key] = int(val)
                elif isinstance(val, (np.floating, float)):
                    entry[key] = None if (val != val) else float(val)
                elif isinstance(val, list):
                    entry[key] = [
                        float(v) if isinstance(v, (np.floating,)) else
                        bool(v) if isinstance(v, (np.bool_,)) else
                        int(v) if isinstance(v, (np.integer,)) else v
                        for v in val
                    ]
            snapshot["coins"].append(entry)
        return snapshot


# ── Routes ──────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return HTML_PAGE


@app.route("/stream")
def stream():
    def generate():
        while True:
            snapshot = get_snapshot()
            yield f"data: {json.dumps(snapshot)}\n\n"
            time.sleep(2)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/data")
def api_data():
    return json.dumps(get_snapshot()), 200, {"Content-Type": "application/json"}


# ── HTML ────────────────────────────────────────────────────────────────────

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crypto Alerts</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }

:root {
    --bg: #0d1117;
    --card: #161b22;
    --border: rgba(255,255,255,0.08);
    --text: #e6edf3;
    --dim: #8b949e;
    --green: #3fb950;
    --red: #f85149;
    --yellow: #d29922;
    --cyan: #58a6ff;
    --purple: #bc8cff;
}

body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    overflow-x: hidden;
}

header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 24px;
    background: linear-gradient(135deg, #161b22 0%, #1a1f2b 100%);
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
    gap: 12px;
}

header .title {
    font-size: 1.25rem;
    font-weight: 700;
    background: linear-gradient(90deg, var(--cyan), var(--purple));
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}

header .portfolio {
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--green);
}

header .meta {
    display: flex;
    align-items: center;
    gap: 12px;
    color: var(--dim);
    font-size: 0.85rem;
}

.status-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--green);
    display: inline-block;
    animation: pulse-dot 2s infinite;
}

.status-dot.offline { background: var(--red); animation: none; }

@keyframes pulse-dot {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
}

/* Cards Grid */
main {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
    gap: 20px;
    padding: 20px 24px;
}

.card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 24px;
    animation: fadeIn 0.4s ease;
    transition: border-color 0.3s ease, box-shadow 0.3s ease;
}

.card:hover {
    border-color: rgba(255,255,255,0.15);
}

.card.glow-green { box-shadow: 0 0 20px rgba(63,185,80,0.15); border-color: rgba(63,185,80,0.3); }
.card.glow-red { box-shadow: 0 0 20px rgba(248,81,73,0.15); border-color: rgba(248,81,73,0.3); }
.card.glow-yellow { box-shadow: 0 0 20px rgba(210,153,34,0.15); border-color: rgba(210,153,34,0.3); }

@keyframes fadeIn {
    from { opacity: 0; transform: translateY(12px); }
    to { opacity: 1; transform: translateY(0); }
}

/* Card Header */
.card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
}

.coin-name {
    font-size: 1.4rem;
    font-weight: 700;
}

.alert-badge {
    padding: 4px 12px;
    border-radius: 20px;
    font-size: 0.75rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

/* Price */
.price-row {
    display: flex;
    align-items: baseline;
    gap: 10px;
    margin-bottom: 4px;
}

.price-value {
    font-size: 1.8rem;
    font-weight: 700;
    transition: color 0.3s;
}

.price-change {
    font-size: 0.9rem;
    font-weight: 600;
}

.flash-green { animation: flashG 0.6s; }
.flash-red { animation: flashR 0.6s; }

@keyframes flashG { 0% { color: var(--green); } 100% { color: var(--text); } }
@keyframes flashR { 0% { color: var(--red); } 100% { color: var(--text); } }

/* Sparkline */
.sparkline-wrap {
    height: 45px;
    margin: 8px 0 16px;
    border-radius: 8px;
    overflow: hidden;
}

.sparkline-wrap canvas { width: 100%; height: 100%; display: block; }

/* Holdings */
.holdings {
    display: flex;
    justify-content: space-between;
    padding: 10px 14px;
    background: rgba(255,255,255,0.03);
    border-radius: 10px;
    margin-bottom: 16px;
    font-size: 0.9rem;
}

.holdings .label { color: var(--dim); }
.holdings .val { font-weight: 600; }

/* Indicators section */
.indicators {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-bottom: 16px;
}

.ind-row {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 0.85rem;
}

.ind-label {
    width: 70px;
    color: var(--dim);
    flex-shrink: 0;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

/* Bar component */
.bar-track {
    flex: 1;
    height: 8px;
    background: rgba(255,255,255,0.08);
    border-radius: 4px;
    overflow: hidden;
    position: relative;
}

.bar-fill {
    height: 100%;
    border-radius: 4px;
    transition: width 0.8s ease, background 0.5s ease;
}

.ind-value {
    min-width: 36px;
    font-weight: 600;
    text-align: right;
}

.ind-tag {
    font-size: 0.7rem;
    font-weight: 600;
    padding: 2px 6px;
    border-radius: 4px;
    text-transform: uppercase;
}

/* EMA row */
.ema-row {
    display: flex;
    gap: 16px;
    font-size: 0.85rem;
    flex-wrap: wrap;
}

.ema-row span { color: var(--dim); }
.ema-row .ema-val { color: var(--cyan); font-weight: 600; }
.trend-badge {
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
}

/* BB track */
.bb-track {
    flex: 1;
    height: 18px;
    background: rgba(255,255,255,0.06);
    border-radius: 9px;
    position: relative;
    display: flex;
    align-items: center;
}

.bb-mid {
    position: absolute;
    left: 50%;
    top: 2px; bottom: 2px;
    width: 1px;
    background: rgba(255,255,255,0.15);
}

.bb-dot {
    position: absolute;
    width: 12px; height: 12px;
    border-radius: 50%;
    top: 3px;
    transition: left 0.8s ease, background 0.5s ease;
    box-shadow: 0 0 6px rgba(255,255,255,0.3);
}

.bb-labels {
    display: flex;
    justify-content: space-between;
    font-size: 0.7rem;
    color: var(--dim);
    min-width: 130px;
    flex-shrink: 0;
}

/* MACD */
.macd-val { font-weight: 600; }
.macd-tag {
    font-size: 0.7rem;
    padding: 2px 6px;
    border-radius: 4px;
    font-weight: 600;
    background: rgba(63,185,80,0.15);
    color: var(--green);
}

/* Buy Target */
.buy-section {
    padding: 12px 14px;
    background: rgba(63,185,80,0.05);
    border: 1px solid rgba(63,185,80,0.15);
    border-radius: 10px;
    margin-bottom: 16px;
}

.buy-price-row {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin-bottom: 6px;
}

.buy-price-val {
    font-size: 1.1rem;
    font-weight: 700;
    color: var(--green);
}

.buy-discount { font-size: 0.85rem; font-weight: 600; }

.buy-details {
    display: flex;
    gap: 16px;
    font-size: 0.75rem;
    color: var(--dim);
}

/* Bottom Score */
.bottom-section {
    padding: 14px;
    background: rgba(255,255,255,0.02);
    border: 1px solid var(--border);
    border-radius: 10px;
    margin-bottom: 16px;
}

.bottom-header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
}

.bottom-score-label { font-size: 0.85rem; color: var(--dim); }

.bottom-bar-track {
    flex: 1;
    height: 10px;
    background: rgba(255,255,255,0.08);
    border-radius: 5px;
    overflow: hidden;
}

.bottom-bar-fill {
    height: 100%;
    border-radius: 5px;
    transition: width 0.8s ease, background 0.5s ease;
    background: linear-gradient(90deg, var(--yellow), var(--green));
}

.bottom-score-val {
    font-size: 1rem;
    font-weight: 700;
    min-width: 50px;
    text-align: right;
}

.bottom-status {
    font-size: 0.75rem;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 4px;
}

.bottom-signals {
    display: flex;
    flex-direction: column;
    gap: 4px;
}

.bottom-signal {
    font-size: 0.8rem;
    color: var(--dim);
    padding-left: 12px;
    position: relative;
}

.bottom-signal::before {
    content: '';
    position: absolute;
    left: 0; top: 7px;
    width: 5px; height: 5px;
    border-radius: 50%;
    background: var(--dim);
}

.bottom-signal.active::before { background: var(--green); }
.bottom-signal.active { color: var(--green); }

/* Support levels */
.support-row {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    font-size: 0.8rem;
}

.support-level {
    padding: 3px 8px;
    background: rgba(88,166,255,0.1);
    border-radius: 6px;
    color: var(--cyan);
}

.support-pct { color: var(--dim); font-size: 0.75rem; }

/* Alert box */
.alert-box {
    padding: 12px 14px;
    border-radius: 10px;
    margin-top: 16px;
}

.alert-label {
    font-size: 1rem;
    font-weight: 700;
    margin-bottom: 6px;
}

.alert-reasons {
    font-size: 0.82rem;
    color: rgba(255,255,255,0.8);
    line-height: 1.5;
}

/* Alert History */
.history-section {
    margin: 0 24px 20px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 20px 24px;
}

.history-title {
    font-size: 0.9rem;
    color: var(--dim);
    margin-bottom: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

.history-table { width: 100%; font-size: 0.82rem; }
.history-table th {
    text-align: left;
    color: var(--dim);
    font-weight: 600;
    padding: 6px 8px;
    border-bottom: 1px solid var(--border);
}
.history-table td { padding: 6px 8px; }

/* Footer */
footer {
    padding: 12px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    color: var(--dim);
    font-size: 0.8rem;
    border-top: 1px solid var(--border);
    flex-wrap: wrap;
    gap: 8px;
}

/* Responsive */
@media (max-width: 480px) {
    main { grid-template-columns: 1fr; padding: 12px; gap: 14px; }
    header { padding: 12px 16px; }
    .card { padding: 18px; }
    .coin-name { font-size: 1.2rem; }
    .price-value { font-size: 1.4rem; }
    .history-section { margin: 0 12px 16px; padding: 14px; }
    footer { padding: 10px 16px; }
}
</style>
</head>
<body>

<header>
    <div class="title">Crypto Alerts</div>
    <div class="portfolio" id="portfolio">--</div>
    <div class="meta">
        <span id="clock">--:--:--</span>
        <span class="status-dot" id="status-dot"></span>
        <span id="countdown">--</span>
    </div>
</header>

<main id="cards"></main>

<div class="history-section">
    <div class="history-title">Alert History</div>
    <table class="history-table">
        <thead><tr><th>Time</th><th>Coin</th><th>Alert</th><th>Detail</th></tr></thead>
        <tbody id="history-body"></tbody>
    </table>
</div>

<footer>
    <div id="log-messages"></div>
    <div>Alerts Dashboard</div>
</footer>

<script>
const ALERT_COLORS = {
    "STRONG BUY": "#3fb950", "BUY ZONE": "#2ea043", "BUY SIGNAL": "#58a6ff",
    "DIP ALERT": "#d29922", "OVERBOUGHT": "#f85149", "CAUTION": "#ff6b6b", "WAIT": "#8b949e"
};

const ALERT_BG = {
    "STRONG BUY": "rgba(63,185,80,0.2)", "BUY ZONE": "rgba(46,160,67,0.15)",
    "BUY SIGNAL": "rgba(88,166,255,0.15)", "DIP ALERT": "rgba(210,153,34,0.15)",
    "OVERBOUGHT": "rgba(248,81,73,0.15)", "CAUTION": "rgba(255,107,107,0.15)",
    "WAIT": "rgba(139,148,158,0.1)"
};

let prevPrices = {};

function formatUSD(n) {
    if (n == null) return "--";
    return Math.abs(n) >= 1 ? "$" + n.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})
                             : "$" + n.toFixed(4);
}

function glowClass(alert) {
    if (["STRONG BUY","BUY ZONE","BUY SIGNAL"].includes(alert)) return "glow-green";
    if (["OVERBOUGHT","CAUTION"].includes(alert)) return "glow-red";
    if (alert === "DIP ALERT") return "glow-yellow";
    return "";
}

function drawSparkline(canvas, prices) {
    if (!prices || prices.length < 2) return;
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    const w = rect.width, h = rect.height;
    ctx.clearRect(0, 0, w, h);

    const min = Math.min(...prices), max = Math.max(...prices);
    const range = max - min || 1;

    const pts = prices.map((p, i) => ({
        x: (i / (prices.length - 1)) * w,
        y: h - ((p - min) / range) * (h - 6) - 3
    }));

    // Area fill
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, "rgba(88,166,255,0.25)");
    grad.addColorStop(1, "rgba(88,166,255,0)");
    ctx.beginPath();
    ctx.moveTo(0, h);
    pts.forEach(p => ctx.lineTo(p.x, p.y));
    ctx.lineTo(w, h);
    ctx.fillStyle = grad;
    ctx.fill();

    // Line
    ctx.beginPath();
    pts.forEach((p, i) => i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y));
    ctx.strokeStyle = "#58a6ff";
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.stroke();

    // End dot
    const last = pts[pts.length - 1];
    ctx.beginPath();
    ctx.arc(last.x, last.y, 3, 0, Math.PI * 2);
    ctx.fillStyle = "#58a6ff";
    ctx.fill();
}

function rsiColor(rsi) {
    if (rsi <= 30) return "var(--green)";
    if (rsi >= 70) return "var(--red)";
    return "var(--yellow)";
}

function bottomColor(score) {
    if (score >= 7) return "var(--green)";
    if (score >= 4) return "var(--yellow)";
    return "var(--dim)";
}

function bottomLabel(score) {
    if (score >= 7) return "LIKELY BOTTOM";
    if (score >= 4) return "GETTING CLOSE";
    return "NOT YET";
}

function bbDotColor(pct) {
    if (pct <= 0.2) return "var(--green)";
    if (pct >= 0.8) return "var(--red)";
    return "var(--yellow)";
}

function renderCoinCard(coin) {
    const id = "card-" + coin.coin;
    let card = document.getElementById(id);
    const isNew = !card;
    if (isNew) {
        card = document.createElement("div");
        card.className = "card";
        card.id = id;
        document.getElementById("cards").appendChild(card);
    }

    const alert = coin.alert || "WAIT";
    const alertColor = ALERT_COLORS[alert] || "#8b949e";
    const alertBg = ALERT_BG[alert] || "rgba(139,148,158,0.1)";
    card.className = "card " + glowClass(alert);

    const rsi = coin.rsi != null ? coin.rsi : 0;
    const rsi1h = coin.rsi_1h;
    const bbPct = coin.bb_pct != null ? coin.bb_pct : 0.5;
    const drop = coin.drop_pct || 0;
    const vol = coin.volume_ratio || 0;
    const bscore = coin.bottom_score || 0;
    const emaF = coin.ema_fast;
    const emaS = coin.ema_slow;
    const trend = emaF > emaS ? "bullish" : "bearish";
    const trendColor = trend === "bullish" ? "var(--green)" : "var(--red)";

    // Price change animation
    let priceClass = "";
    if (prevPrices[coin.coin] !== undefined && prevPrices[coin.coin] !== coin.price) {
        priceClass = coin.price > prevPrices[coin.coin] ? "flash-green" : "flash-red";
    }
    prevPrices[coin.coin] = coin.price;

    let dropHtml;
    if (drop < -1) dropHtml = '<span class="price-change" style="color:var(--red)">' + drop.toFixed(1) + '% from high</span>';
    else if (drop > -0.5) dropHtml = '<span class="price-change" style="color:var(--green)">near high</span>';
    else dropHtml = '<span class="price-change" style="color:var(--dim)">' + drop.toFixed(1) + '%</span>';

    // RSI label
    let rsiLabel = "";
    if (rsi <= 30) rsiLabel = '<span class="ind-tag" style="background:rgba(63,185,80,0.15);color:var(--green)">OVERSOLD</span>';
    else if (rsi >= 70) rsiLabel = '<span class="ind-tag" style="background:rgba(248,81,73,0.15);color:var(--red)">OVERBOUGHT</span>';

    // 1h RSI
    let rsi1hHtml = "";
    if (rsi1h != null) {
        const r1c = rsi1h <= 35 ? "var(--green)" : rsi1h >= 65 ? "var(--red)" : "var(--yellow)";
        let r1tag = "";
        if (rsi1h <= 35) r1tag = '<span class="ind-tag" style="background:rgba(63,185,80,0.15);color:var(--green)">OVERSOLD 1H</span>';
        else if (rsi1h >= 65) r1tag = '<span class="ind-tag" style="background:rgba(248,81,73,0.15);color:var(--red)">OVERBOUGHT 1H</span>';
        rsi1hHtml = '<div class="ind-row"><span class="ind-label">1h RSI</span><div class="bar-track"><div class="bar-fill" style="width:' + rsi1h + '%;background:' + r1c + '"></div></div><span class="ind-value" style="color:' + r1c + '">' + rsi1h.toFixed(0) + '</span>' + r1tag + '</div>';
    }

    // Volume
    let volTag = vol >= 2.0 ? '<span class="ind-tag" style="background:rgba(210,153,34,0.15);color:var(--yellow)">HIGH</span>' : "";

    // MACD
    const macdH = coin.macd_hist || 0;
    const macdUp = coin.macd_turning_up;
    const macdColor = macdH > 0 ? "var(--green)" : "var(--red)";
    const macdArrow = macdH > 0 ? "&#9650;" : "&#9660;";
    let macdTag = macdUp ? '<span class="macd-tag">TURNING UP</span>' : "";

    // Support levels
    let supHtml = "";
    if (coin.support_levels && coin.support_levels.length > 0) {
        supHtml = '<div class="ind-row"><span class="ind-label">Support</span><div class="support-row">';
        coin.support_levels.forEach(s => {
            const pctAway = ((s - coin.price) / coin.price * 100).toFixed(1);
            supHtml += '<span class="support-level">' + formatUSD(s) + ' <span class="support-pct">(' + pctAway + '%)</span></span>';
        });
        supHtml += '</div></div>';
    }

    // Buy section
    const buyDiscount = coin.buy_discount || 0;
    const discColor = buyDiscount < 0 ? "var(--green)" : "var(--red)";

    // Bottom signals
    let sigHtml = "";
    if (coin.bottom_signals) {
        coin.bottom_signals.forEach(s => {
            const active = s !== "No bottom signals yet" ? "active" : "";
            sigHtml += '<div class="bottom-signal ' + active + '">' + s + '</div>';
        });
    }

    // Reasons
    let reasonsHtml = "";
    if (coin.reasons) {
        reasonsHtml = coin.reasons.map(r => "&#8226; " + r).join("<br>");
    }

    card.innerHTML = `
        <div class="card-header">
            <span class="coin-name">${coin.coin}</span>
            <span class="alert-badge" style="background:${alertBg};color:${alertColor}">${alert}</span>
        </div>

        <div class="price-row">
            <span class="price-value ${priceClass}">${formatUSD(coin.price)}</span>
            ${dropHtml}
        </div>

        <div class="sparkline-wrap"><canvas id="spark-${coin.coin}"></canvas></div>

        <div class="holdings">
            <span><span class="label">Holdings </span><span class="val">${coin.holdings} ${coin.coin}</span></span>
            <span class="val">${formatUSD(coin.value)}</span>
        </div>

        <div class="indicators">
            <div class="ema-row">
                <span>EMA(9) <span class="ema-val">${formatUSD(emaF)}</span></span>
                <span>EMA(21) <span class="ema-val">${formatUSD(emaS)}</span></span>
                <span class="trend-badge" style="background:${trendColor}22;color:${trendColor}">${trend}</span>
            </div>

            <div class="ind-row">
                <span class="ind-label">RSI</span>
                <div class="bar-track"><div class="bar-fill" style="width:${rsi}%;background:${rsiColor(rsi)}"></div></div>
                <span class="ind-value" style="color:${rsiColor(rsi)}">${rsi.toFixed(0)}</span>
                ${rsiLabel}
            </div>

            ${rsi1hHtml}

            <div class="ind-row">
                <span class="ind-label">Volume</span>
                <span class="ind-value">${vol.toFixed(1)}x</span>
                ${volTag}
            </div>

            <div class="ind-row">
                <span class="ind-label">BB</span>
                <div class="bb-track">
                    <div class="bb-mid"></div>
                    <div class="bb-dot" style="left:calc(${Math.max(0,Math.min(1,bbPct))*100}% - 6px);background:${bbDotColor(bbPct)}"></div>
                </div>
                <div class="bb-labels">
                    <span>${formatUSD(coin.bb_lower)}</span>
                    <span>${formatUSD(coin.bb_upper)}</span>
                </div>
            </div>

            <div class="ind-row">
                <span class="ind-label">MACD</span>
                <span class="macd-val" style="color:${macdColor}">${macdArrow} ${macdH.toFixed(4)}</span>
                ${macdTag}
            </div>

            ${supHtml}
        </div>

        <div class="buy-section">
            <div class="buy-price-row">
                <span style="color:var(--dim);font-size:0.85rem">Buy at</span>
                <span class="buy-price-val">${formatUSD(coin.buy_price)}</span>
                <span class="buy-discount" style="color:${discColor}">(${buyDiscount >= 0 ? "+" : ""}${buyDiscount.toFixed(1)}%)</span>
            </div>
            <div class="buy-details">
                <span>EMA support: ${formatUSD(coin.buy_support)}</span>
                <span>Recent low: ${formatUSD(coin.buy_low)}</span>
            </div>
        </div>

        <div class="bottom-section">
            <div class="bottom-header">
                <span class="bottom-score-label">Bottom</span>
                <div class="bottom-bar-track"><div class="bottom-bar-fill" style="width:${bscore*10}%"></div></div>
                <span class="bottom-score-val" style="color:${bottomColor(bscore)}">${bscore}/10</span>
                <span class="bottom-status" style="background:${bottomColor(bscore)}22;color:${bottomColor(bscore)}">${bottomLabel(bscore)}</span>
            </div>
            <div class="bottom-signals">${sigHtml}</div>
        </div>

        <div class="alert-box" style="background:${alertBg}">
            <div class="alert-label" style="color:${alertColor}">${alert}</div>
            <div class="alert-reasons">${reasonsHtml}</div>
        </div>
    `;

    // Draw sparkline
    const sparkCanvas = document.getElementById("spark-" + coin.coin);
    if (sparkCanvas && coin.price_history) {
        requestAnimationFrame(() => drawSparkline(sparkCanvas, coin.price_history));
    }
}

function renderHistory(alertLog) {
    const tbody = document.getElementById("history-body");
    if (!alertLog || alertLog.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" style="color:var(--dim);text-align:center;padding:12px">No alerts yet</td></tr>';
        return;
    }
    tbody.innerHTML = alertLog.map(e => {
        const c = ALERT_COLORS[e.alert] || "#8b949e";
        return '<tr><td style="color:var(--dim)">' + e.time + '</td><td style="font-weight:600">' + e.coin + '</td><td style="color:' + c + ';font-weight:600">' + e.alert + '</td><td style="color:var(--dim)">' + e.reason + '</td></tr>';
    }).join("");
}

function renderDashboard(data) {
    document.getElementById("portfolio").textContent = formatUSD(data.total_value);
    document.getElementById("clock").textContent = data.timestamp;
    document.getElementById("countdown").textContent = "Next: " + data.poll_countdown + "s";
    document.getElementById("status-dot").className = "status-dot";

    if (data.coins) data.coins.forEach(c => renderCoinCard(c));
    renderHistory(data.alert_log);

    const logEl = document.getElementById("log-messages");
    if (data.log_messages) logEl.textContent = data.log_messages.join(" | ");
}

// SSE connection
const evtSource = new EventSource("/stream");
evtSource.onmessage = function(e) {
    try { renderDashboard(JSON.parse(e.data)); }
    catch(err) { console.error("Parse error:", err); }
};
evtSource.onerror = function() {
    document.getElementById("status-dot").className = "status-dot offline";
};
</script>
</body>
</html>"""


# ── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()
    print("Starting web dashboard on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
