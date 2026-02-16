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
from flask import Flask, Response, request

from alerts import Config, ExchangeClient, AlertAnalyzer, CONFIG_PATH

# ── App & State ─────────────────────────────────────────────────────────────

app = Flask(__name__)

data_lock = threading.Lock()
coin_data = {}
price_histories = {}
alert_log = collections.deque(maxlen=50)
log_messages = collections.deque(maxlen=50)
last_update_time = ""
poll_countdown = 0
reload_event = threading.Event()


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
        # Check if config was changed via the web UI
        if reload_event.is_set():
            reload_event.clear()
            config = Config()
            analyzer = AlertAnalyzer(config)
            # Init price histories for any new pairs
            for w in config.watchlist:
                if w["pair"] not in price_histories:
                    price_histories[w["pair"]] = collections.deque(maxlen=40)
            # Remove data for deleted pairs
            current_pairs = {w["pair"] for w in config.watchlist}
            for pair in list(coin_data.keys()):
                if pair not in current_pairs:
                    with data_lock:
                        coin_data.pop(pair, None)
                        price_histories.pop(pair, None)
            with data_lock:
                coins_str = ", ".join(w["pair"].replace("/USD", "") for w in config.watchlist)
                log_messages.append(f"Config reloaded: {coins_str}")
            last_poll = 0.0  # Force immediate poll

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
        try:
            watchlist = Config().watchlist
        except Exception:
            watchlist = []
        snapshot = {
            "coins": [],
            "watchlist": watchlist,
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


@app.route("/api/watchlist")
def api_watchlist():
    config = Config()
    return json.dumps(config.watchlist), 200, {"Content-Type": "application/json"}


@app.route("/api/watchlist", methods=["POST"])
def api_add_coin():
    """Add a new coin. Body: {"pair": "BTC/USD", "holdings": 0.5}"""
    body = request.get_json(silent=True)
    if not body or "pair" not in body:
        return json.dumps({"error": "pair is required"}), 400, {"Content-Type": "application/json"}

    pair = body["pair"].upper().strip()
    if not pair.endswith("/USD"):
        pair = pair + "/USD"
    holdings = float(body.get("holdings", 0))

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    # Check if already exists
    for w in cfg["watchlist"]:
        if w["pair"] == pair:
            return json.dumps({"error": f"{pair} already in watchlist"}), 400, {"Content-Type": "application/json"}

    cfg["watchlist"].append({"pair": pair, "holdings": holdings})
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    reload_event.set()
    return json.dumps({"ok": True, "watchlist": cfg["watchlist"]}), 200, {"Content-Type": "application/json"}


@app.route("/api/watchlist", methods=["PUT"])
def api_edit_coin():
    """Edit holdings. Body: {"pair": "XRP/USD", "holdings": 800}"""
    body = request.get_json(silent=True)
    if not body or "pair" not in body or "holdings" not in body:
        return json.dumps({"error": "pair and holdings required"}), 400, {"Content-Type": "application/json"}

    pair = body["pair"].upper().strip()
    holdings = float(body["holdings"])

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    found = False
    for w in cfg["watchlist"]:
        if w["pair"] == pair:
            w["holdings"] = holdings
            found = True
            break

    if not found:
        return json.dumps({"error": f"{pair} not in watchlist"}), 404, {"Content-Type": "application/json"}

    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    reload_event.set()
    return json.dumps({"ok": True, "watchlist": cfg["watchlist"]}), 200, {"Content-Type": "application/json"}


@app.route("/api/watchlist", methods=["DELETE"])
def api_remove_coin():
    """Remove a coin. Body: {"pair": "BTC/USD"}"""
    body = request.get_json(silent=True)
    if not body or "pair" not in body:
        return json.dumps({"error": "pair is required"}), 400, {"Content-Type": "application/json"}

    pair = body["pair"].upper().strip()

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    before = len(cfg["watchlist"])
    cfg["watchlist"] = [w for w in cfg["watchlist"] if w["pair"] != pair]

    if len(cfg["watchlist"]) == before:
        return json.dumps({"error": f"{pair} not in watchlist"}), 404, {"Content-Type": "application/json"}

    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    reload_event.set()
    return json.dumps({"ok": True, "watchlist": cfg["watchlist"]}), 200, {"Content-Type": "application/json"}


# ── HTML ────────────────────────────────────────────────────────────────────

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
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

/* Settings Modal */
.settings-btn {
    background: rgba(255,255,255,0.08);
    border: 1px solid var(--border);
    color: var(--dim);
    padding: 6px 12px;
    border-radius: 8px;
    cursor: pointer;
    font-size: 0.85rem;
    transition: all 0.2s;
}

.settings-btn:hover { background: rgba(255,255,255,0.12); color: var(--text); }

.modal-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.7);
    z-index: 100;
    justify-content: center;
    align-items: center;
    padding: 20px;
}

.modal-overlay.open { display: flex; }

.modal {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 28px;
    width: 100%;
    max-width: 440px;
    max-height: 85vh;
    overflow-y: auto;
    animation: fadeIn 0.3s ease;
}

.modal h2 {
    font-size: 1.1rem;
    margin-bottom: 20px;
    color: var(--text);
}

.modal-close {
    float: right;
    background: none;
    border: none;
    color: var(--dim);
    font-size: 1.5rem;
    cursor: pointer;
    line-height: 1;
    padding: 0 4px;
}

.modal-close:hover { color: var(--text); }

.wl-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 14px;
    background: rgba(255,255,255,0.03);
    border: 1px solid var(--border);
    border-radius: 10px;
    margin-bottom: 10px;
}

.wl-item .wl-pair {
    font-weight: 600;
    min-width: 80px;
}

.wl-item input {
    width: 90px;
    background: rgba(255,255,255,0.06);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    padding: 6px 10px;
    font-size: 0.85rem;
}

.wl-item input:focus {
    outline: none;
    border-color: var(--cyan);
}

.wl-btn {
    padding: 6px 12px;
    border-radius: 6px;
    border: none;
    cursor: pointer;
    font-size: 0.8rem;
    font-weight: 600;
    transition: all 0.2s;
}

.wl-btn.save { background: rgba(63,185,80,0.15); color: var(--green); }
.wl-btn.save:hover { background: rgba(63,185,80,0.3); }
.wl-btn.remove { background: rgba(248,81,73,0.1); color: var(--red); }
.wl-btn.remove:hover { background: rgba(248,81,73,0.25); }

.add-form {
    display: flex;
    gap: 8px;
    margin-top: 16px;
    flex-wrap: wrap;
}

.add-form input {
    flex: 1;
    min-width: 80px;
    background: rgba(255,255,255,0.06);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    padding: 10px 12px;
    font-size: 0.9rem;
}

.add-form input:focus { outline: none; border-color: var(--cyan); }

.add-form input::placeholder { color: var(--dim); }

.add-btn {
    padding: 10px 20px;
    background: linear-gradient(135deg, var(--cyan), var(--purple));
    border: none;
    border-radius: 8px;
    color: white;
    font-weight: 700;
    font-size: 0.9rem;
    cursor: pointer;
    transition: opacity 0.2s;
}

.add-btn:hover { opacity: 0.85; }

.modal-msg {
    font-size: 0.82rem;
    margin-top: 10px;
    min-height: 20px;
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
        <button class="settings-btn" onclick="openSettings()">Settings</button>
    </div>
</header>

<div class="modal-overlay" id="modal-overlay" onclick="if(event.target===this)closeSettings()">
    <div class="modal">
        <button class="modal-close" onclick="closeSettings()">&times;</button>
        <h2>Watchlist</h2>
        <div id="wl-list"></div>
        <div class="add-form">
            <input type="text" id="add-pair" placeholder="Coin (e.g. BTC)">
            <input type="number" id="add-holdings" placeholder="Holdings" step="any" min="0">
            <button class="add-btn" onclick="addCoin()">Add</button>
        </div>
        <div class="modal-msg" id="modal-msg"></div>
    </div>
</div>

<main id="cards"><div style="text-align:center;padding:40px;color:#8b949e">Loading data...</div></main>

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
var ALERT_COLORS = {
    "STRONG BUY": "#3fb950", "BUY ZONE": "#2ea043", "BUY SIGNAL": "#58a6ff",
    "DIP ALERT": "#d29922", "OVERBOUGHT": "#f85149", "CAUTION": "#ff6b6b", "WAIT": "#8b949e"
};

var ALERT_BG = {
    "STRONG BUY": "rgba(63,185,80,0.2)", "BUY ZONE": "rgba(46,160,67,0.15)",
    "BUY SIGNAL": "rgba(88,166,255,0.15)", "DIP ALERT": "rgba(210,153,34,0.15)",
    "OVERBOUGHT": "rgba(248,81,73,0.15)", "CAUTION": "rgba(255,107,107,0.15)",
    "WAIT": "rgba(139,148,158,0.1)"
};

var prevPrices = {};
var currentWatchlist = [];
var firstLoad = true;

function formatUSD(n) {
    if (n == null) return "--";
    if (Math.abs(n) >= 1) return "$" + n.toFixed(2);
    return "$" + n.toFixed(4);
}

function glowClass(a) {
    if (a === "STRONG BUY" || a === "BUY ZONE" || a === "BUY SIGNAL") return "glow-green";
    if (a === "OVERBOUGHT" || a === "CAUTION") return "glow-red";
    if (a === "DIP ALERT") return "glow-yellow";
    return "";
}

function rsiColor(rsi) {
    if (rsi <= 30) return "#3fb950";
    if (rsi >= 70) return "#f85149";
    return "#d29922";
}

function bottomColor(s) {
    if (s >= 7) return "#3fb950";
    if (s >= 4) return "#d29922";
    return "#8b949e";
}

function bottomLabel(s) {
    if (s >= 7) return "LIKELY BOTTOM";
    if (s >= 4) return "GETTING CLOSE";
    return "NOT YET";
}

function bbDotColor(p) {
    if (p <= 0.2) return "#3fb950";
    if (p >= 0.8) return "#f85149";
    return "#d29922";
}

function drawSparkline(canvas, prices) {
    if (!prices || prices.length < 2) return;
    var dpr = window.devicePixelRatio || 1;
    var rect = canvas.getBoundingClientRect();
    if (rect.width === 0) return;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    var ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    var w = rect.width, h = rect.height;
    ctx.clearRect(0, 0, w, h);

    var min = Math.min.apply(null, prices);
    var max = Math.max.apply(null, prices);
    var range = max - min || 1;

    // Area fill
    var grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, "rgba(88,166,255,0.25)");
    grad.addColorStop(1, "rgba(88,166,255,0)");
    ctx.beginPath();
    ctx.moveTo(0, h);
    for (var i = 0; i < prices.length; i++) {
        var x = (i / (prices.length - 1)) * w;
        var y = h - ((prices[i] - min) / range) * (h - 6) - 3;
        ctx.lineTo(x, y);
    }
    ctx.lineTo(w, h);
    ctx.fillStyle = grad;
    ctx.fill();

    // Line
    ctx.beginPath();
    for (var i = 0; i < prices.length; i++) {
        var x = (i / (prices.length - 1)) * w;
        var y = h - ((prices[i] - min) / range) * (h - 6) - 3;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = "#58a6ff";
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.stroke();
}

function renderCoinCard(coin) {
    var id = "card-" + coin.coin;
    var card = document.getElementById(id);
    if (!card) {
        card = document.createElement("div");
        card.className = "card";
        card.id = id;
        document.getElementById("cards").appendChild(card);
    }

    var alert = coin.alert || "WAIT";
    var alertColor = ALERT_COLORS[alert] || "#8b949e";
    var alertBg = ALERT_BG[alert] || "rgba(139,148,158,0.1)";
    card.className = "card " + glowClass(alert);

    var rsi = coin.rsi != null ? coin.rsi : 0;
    var rsi1h = coin.rsi_1h;
    var bbPct = coin.bb_pct != null ? coin.bb_pct : 0.5;
    var drop = coin.drop_pct || 0;
    var vol = coin.volume_ratio || 0;
    var bscore = coin.bottom_score || 0;
    var emaF = coin.ema_fast;
    var emaS = coin.ema_slow;
    var trend = emaF > emaS ? "bullish" : "bearish";
    var trendColor = trend === "bullish" ? "#3fb950" : "#f85149";

    var dropText = "";
    if (drop < -1) dropText = '<span class="price-change" style="color:#f85149">' + drop.toFixed(1) + '% from high</span>';
    else if (drop > -0.5) dropText = '<span class="price-change" style="color:#3fb950">near high</span>';
    else dropText = '<span class="price-change" style="color:#8b949e">' + drop.toFixed(1) + '%</span>';

    var rsiTag = "";
    if (rsi <= 30) rsiTag = '<span class="ind-tag" style="background:rgba(63,185,80,0.15);color:#3fb950">OVERSOLD</span>';
    else if (rsi >= 70) rsiTag = '<span class="ind-tag" style="background:rgba(248,81,73,0.15);color:#f85149">OVERBOUGHT</span>';

    var rsi1hHtml = "";
    if (rsi1h != null) {
        var r1c = rsi1h <= 35 ? "#3fb950" : rsi1h >= 65 ? "#f85149" : "#d29922";
        var r1tag = "";
        if (rsi1h <= 35) r1tag = '<span class="ind-tag" style="background:rgba(63,185,80,0.15);color:#3fb950">OVERSOLD 1H</span>';
        else if (rsi1h >= 65) r1tag = '<span class="ind-tag" style="background:rgba(248,81,73,0.15);color:#f85149">OVERBOUGHT 1H</span>';
        rsi1hHtml = '<div class="ind-row"><span class="ind-label">1h RSI</span><div class="bar-track"><div class="bar-fill" style="width:' + rsi1h + '%;background:' + r1c + '"></div></div><span class="ind-value" style="color:' + r1c + '">' + rsi1h.toFixed(0) + '</span>' + r1tag + '</div>';
    }

    var volTag = vol >= 2.0 ? '<span class="ind-tag" style="background:rgba(210,153,34,0.15);color:#d29922">HIGH</span>' : "";

    var macdH = coin.macd_hist || 0;
    var macdColor = macdH > 0 ? "#3fb950" : "#f85149";
    var macdArrow = macdH > 0 ? "&#9650;" : "&#9660;";
    var macdTag = coin.macd_turning_up ? '<span class="macd-tag">TURNING UP</span>' : "";

    var supHtml = "";
    if (coin.support_levels && coin.support_levels.length > 0) {
        supHtml = '<div class="ind-row"><span class="ind-label">Support</span><div class="support-row">';
        for (var i = 0; i < coin.support_levels.length; i++) {
            var s = coin.support_levels[i];
            var pctAway = ((s - coin.price) / coin.price * 100).toFixed(1);
            supHtml += '<span class="support-level">' + formatUSD(s) + ' <span class="support-pct">(' + pctAway + '%)</span></span>';
        }
        supHtml += '</div></div>';
    }

    var buyDiscount = coin.buy_discount || 0;
    var discColor = buyDiscount < 0 ? "#3fb950" : "#f85149";

    var sigHtml = "";
    if (coin.bottom_signals) {
        for (var i = 0; i < coin.bottom_signals.length; i++) {
            var active = coin.bottom_signals[i] !== "No bottom signals yet" ? "active" : "";
            sigHtml += '<div class="bottom-signal ' + active + '">' + coin.bottom_signals[i] + '</div>';
        }
    }

    var reasonsHtml = "";
    if (coin.reasons) {
        for (var i = 0; i < coin.reasons.length; i++) {
            reasonsHtml += "&#8226; " + coin.reasons[i];
            if (i < coin.reasons.length - 1) reasonsHtml += "<br>";
        }
    }

    var bbPos = Math.max(0, Math.min(1, bbPct)) * 100;

    var h = '';
    h += '<div class="card-header">';
    h += '<span class="coin-name">' + coin.coin + '</span>';
    h += '<span class="alert-badge" style="background:' + alertBg + ';color:' + alertColor + '">' + alert + '</span>';
    h += '</div>';

    h += '<div class="price-row">';
    h += '<span class="price-value">' + formatUSD(coin.price) + '</span>';
    h += dropText;
    h += '</div>';

    h += '<div class="sparkline-wrap"><canvas id="spark-' + coin.coin + '"></canvas></div>';

    h += '<div class="holdings">';
    h += '<span><span class="label">Holdings </span><span class="val">' + coin.holdings + ' ' + coin.coin + '</span></span>';
    h += '<span class="val">' + formatUSD(coin.value) + '</span>';
    h += '</div>';

    h += '<div class="indicators">';
    h += '<div class="ema-row">';
    h += '<span>EMA(9) <span class="ema-val">' + formatUSD(emaF) + '</span></span>';
    h += '<span>EMA(21) <span class="ema-val">' + formatUSD(emaS) + '</span></span>';
    h += '<span class="trend-badge" style="background:' + trendColor + '22;color:' + trendColor + '">' + trend + '</span>';
    h += '</div>';

    h += '<div class="ind-row"><span class="ind-label">RSI</span>';
    h += '<div class="bar-track"><div class="bar-fill" style="width:' + rsi + '%;background:' + rsiColor(rsi) + '"></div></div>';
    h += '<span class="ind-value" style="color:' + rsiColor(rsi) + '">' + rsi.toFixed(0) + '</span>';
    h += rsiTag + '</div>';

    h += rsi1hHtml;

    h += '<div class="ind-row"><span class="ind-label">Volume</span>';
    h += '<span class="ind-value">' + vol.toFixed(1) + 'x</span>' + volTag + '</div>';

    h += '<div class="ind-row"><span class="ind-label">BB</span>';
    h += '<div class="bb-track"><div class="bb-mid"></div>';
    h += '<div class="bb-dot" style="left:calc(' + bbPos + '% - 6px);background:' + bbDotColor(bbPct) + '"></div></div>';
    h += '<div class="bb-labels"><span>' + formatUSD(coin.bb_lower) + '</span><span>' + formatUSD(coin.bb_upper) + '</span></div></div>';

    h += '<div class="ind-row"><span class="ind-label">MACD</span>';
    h += '<span class="macd-val" style="color:' + macdColor + '">' + macdArrow + ' ' + macdH.toFixed(4) + '</span>';
    h += macdTag + '</div>';

    h += supHtml;
    h += '</div>';

    h += '<div class="buy-section">';
    h += '<div class="buy-price-row">';
    h += '<span style="color:#8b949e;font-size:0.85rem">Buy at</span>';
    h += '<span class="buy-price-val">' + formatUSD(coin.buy_price) + '</span>';
    h += '<span class="buy-discount" style="color:' + discColor + '">(' + (buyDiscount >= 0 ? "+" : "") + buyDiscount.toFixed(1) + '%)</span>';
    h += '</div>';
    h += '<div class="buy-details"><span>EMA support: ' + formatUSD(coin.buy_support) + '</span>';
    h += '<span>Recent low: ' + formatUSD(coin.buy_low) + '</span></div></div>';

    h += '<div class="bottom-section">';
    h += '<div class="bottom-header">';
    h += '<span class="bottom-score-label">Bottom</span>';
    h += '<div class="bottom-bar-track"><div class="bottom-bar-fill" style="width:' + (bscore * 10) + '%"></div></div>';
    h += '<span class="bottom-score-val" style="color:' + bottomColor(bscore) + '">' + bscore + '/10</span>';
    h += '<span class="bottom-status" style="background:' + bottomColor(bscore) + '22;color:' + bottomColor(bscore) + '">' + bottomLabel(bscore) + '</span>';
    h += '</div>';
    h += '<div class="bottom-signals">' + sigHtml + '</div></div>';

    h += '<div class="alert-box" style="background:' + alertBg + '">';
    h += '<div class="alert-label" style="color:' + alertColor + '">' + alert + '</div>';
    h += '<div class="alert-reasons">' + reasonsHtml + '</div></div>';

    card.innerHTML = h;

    setTimeout(function() {
        var sparkCanvas = document.getElementById("spark-" + coin.coin);
        if (sparkCanvas && coin.price_history) drawSparkline(sparkCanvas, coin.price_history);
    }, 50);
}

function renderHistory(alertLog) {
    var tbody = document.getElementById("history-body");
    if (!alertLog || alertLog.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" style="color:#8b949e;text-align:center;padding:12px">No alerts yet</td></tr>';
        return;
    }
    var rows = "";
    for (var i = 0; i < alertLog.length; i++) {
        var e = alertLog[i];
        var c = ALERT_COLORS[e.alert] || "#8b949e";
        rows += '<tr><td style="color:#8b949e">' + e.time + '</td><td style="font-weight:600">' + e.coin + '</td><td style="color:' + c + ';font-weight:600">' + e.alert + '</td><td style="color:#8b949e">' + e.reason + '</td></tr>';
    }
    tbody.innerHTML = rows;
}

function renderDashboard(data) {
    document.getElementById("portfolio").textContent = formatUSD(data.total_value);
    document.getElementById("clock").textContent = data.timestamp;
    document.getElementById("countdown").textContent = "Next: " + data.poll_countdown + "s";
    document.getElementById("status-dot").className = "status-dot";

    if (data.watchlist) currentWatchlist = data.watchlist;

    if (data.coins && data.coins.length > 0) {
        if (firstLoad) {
            document.getElementById("cards").innerHTML = "";
            firstLoad = false;
        }
        for (var i = 0; i < data.coins.length; i++) {
            renderCoinCard(data.coins[i]);
        }
    }

    renderHistory(data.alert_log);

    var logEl = document.getElementById("log-messages");
    if (data.log_messages) logEl.textContent = data.log_messages.join(" | ");
}

// ── Settings ──

function openSettings() {
    document.getElementById("modal-overlay").classList.add("open");
    renderWatchlist();
}

function closeSettings() {
    document.getElementById("modal-overlay").classList.remove("open");
}

function renderWatchlist() {
    var list = document.getElementById("wl-list");
    var html = "";
    for (var i = 0; i < currentWatchlist.length; i++) {
        var w = currentWatchlist[i];
        var coin = w.pair.replace("/USD", "");
        html += '<div class="wl-item">';
        html += '<span class="wl-pair">' + coin + '</span>';
        html += '<input type="number" id="wl-hold-' + i + '" value="' + w.holdings + '" step="any" min="0">';
        html += '<button class="wl-btn save" onclick="editCoin(&quot;' + w.pair + '&quot;,' + i + ')">Save</button>';
        html += '<button class="wl-btn remove" onclick="removeCoin(&quot;' + w.pair + '&quot;)">Remove</button>';
        html += '</div>';
    }
    list.innerHTML = html;
}

function showMsg(msg, color) {
    var el = document.getElementById("modal-msg");
    el.textContent = msg;
    el.style.color = color || "#e6edf3";
    setTimeout(function() { el.textContent = ""; }, 3000);
}

function addCoin() {
    var pairInput = document.getElementById("add-pair");
    var holdInput = document.getElementById("add-holdings");
    var pair = pairInput.value.trim();
    var holdings = parseFloat(holdInput.value) || 0;
    if (!pair) { showMsg("Enter a coin name", "#f85149"); return; }

    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/watchlist");
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.onload = function() {
        var data = JSON.parse(xhr.responseText);
        if (data.error) { showMsg(data.error, "#f85149"); return; }
        currentWatchlist = data.watchlist;
        renderWatchlist();
        pairInput.value = "";
        holdInput.value = "";
        showMsg("Added " + pair.toUpperCase(), "#3fb950");
    };
    xhr.send(JSON.stringify({pair: pair, holdings: holdings}));
}

function editCoin(pair, idx) {
    var input = document.getElementById("wl-hold-" + idx);
    var holdings = parseFloat(input.value) || 0;

    var xhr = new XMLHttpRequest();
    xhr.open("PUT", "/api/watchlist");
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.onload = function() {
        var data = JSON.parse(xhr.responseText);
        if (data.error) { showMsg(data.error, "#f85149"); return; }
        currentWatchlist = data.watchlist;
        renderWatchlist();
        showMsg("Updated " + pair.replace("/USD",""), "#3fb950");
    };
    xhr.send(JSON.stringify({pair: pair, holdings: holdings}));
}

function removeCoin(pair) {
    var coin = pair.replace("/USD", "");
    if (!confirm("Remove " + coin + "?")) return;

    var xhr = new XMLHttpRequest();
    xhr.open("DELETE", "/api/watchlist");
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.onload = function() {
        var data = JSON.parse(xhr.responseText);
        if (data.error) { showMsg(data.error, "#f85149"); return; }
        currentWatchlist = data.watchlist;
        renderWatchlist();
        var cardEl = document.getElementById("card-" + coin);
        if (cardEl) cardEl.remove();
        showMsg("Removed " + coin, "#d29922");
    };
    xhr.send(JSON.stringify({pair: pair}));
}

// ── Data Fetching (polling) ──

function fetchData() {
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/api/data");
    xhr.onload = function() {
        try {
            var data = JSON.parse(xhr.responseText);
            renderDashboard(data);
        } catch(e) {}
    };
    xhr.onerror = function() {
        document.getElementById("status-dot").className = "status-dot offline";
    };
    xhr.send();
}

// Fetch immediately, then every 3 seconds
fetchData();
setInterval(fetchData, 3000);
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
