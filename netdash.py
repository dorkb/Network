#!/home/dork/Documents/Network/.venv/bin/python3
"""netdash.py -- Live network dashboard for Raspberry Pi"""

import collections
import datetime
import re
import signal
import socket
import subprocess
import sys
import time

import psutil
from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ── Constants ────────────────────────────────────────────────────────────────

INTERFACES = ["wlan0", "eth0"]
TICK_INTERVAL = 1.0
SPARKLINE_WIDTH = 40
SPARKLINE_BLOCKS = " ▁▂▃▄▅▆▇█"

# ── Utility Functions ────────────────────────────────────────────────────────


def format_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def format_speed(bps):
    if bps < 1024:
        return f"{bps:.0f} B/s"
    elif bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    else:
        return f"{bps / (1024 * 1024):.1f} MB/s"


def make_sparkline(history, color="green"):
    if not history:
        return Text("")
    max_val = max(history) or 1
    chars = ""
    for v in history:
        idx = min(int((v / max_val) * 8), 8)
        chars += SPARKLINE_BLOCKS[idx]
    return Text(chars, style=color)


def format_uptime(boot_time):
    delta = datetime.datetime.now() - datetime.datetime.fromtimestamp(boot_time)
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours >= 24:
        days = hours // 24
        hours = hours % 24
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m {seconds}s"


def signal_color(dbm):
    if dbm is None:
        return "dim"
    if dbm >= -50:
        return "green"
    if dbm >= -70:
        return "yellow"
    return "red"


# ── Data Collectors ──────────────────────────────────────────────────────────


def collect_header():
    try:
        return {
            "hostname": socket.gethostname(),
            "uptime": format_uptime(psutil.boot_time()),
            "time": datetime.datetime.now().strftime("%H:%M:%S"),
            "date": datetime.datetime.now().strftime("%Y-%m-%d"),
        }
    except Exception:
        return {"hostname": "unknown", "uptime": "?", "time": "?", "date": "?"}


def collect_interfaces():
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        result = []
        for iface in INTERFACES:
            info = {"name": iface, "is_up": False, "ipv4": "-", "mac": "-", "mtu": 0}
            if iface in stats:
                info["is_up"] = stats[iface].isup
                info["mtu"] = stats[iface].mtu
            if iface in addrs:
                for addr in addrs[iface]:
                    if addr.family == socket.AF_INET:
                        info["ipv4"] = addr.address
                    elif addr.family == socket.AF_PACKET:
                        info["mac"] = addr.address
            result.append(info)
        return result
    except Exception:
        return []


def collect_bandwidth(prev_counters, prev_time, speed_history):
    try:
        now_time = time.monotonic()
        elapsed = now_time - prev_time if prev_time else 1.0
        if elapsed <= 0:
            elapsed = 1.0
        now_counters = psutil.net_io_counters(pernic=True)
        result = {}
        for iface in INTERFACES:
            if iface in now_counters:
                nc = now_counters[iface]
                if iface in prev_counters:
                    pc = prev_counters[iface]
                    dl_speed = max(0, (nc.bytes_recv - pc[1])) / elapsed
                    ul_speed = max(0, (nc.bytes_sent - pc[0])) / elapsed
                else:
                    dl_speed = 0.0
                    ul_speed = 0.0
                speed_history[iface]["dl"].append(dl_speed)
                speed_history[iface]["ul"].append(ul_speed)
                result[iface] = {
                    "dl_speed": dl_speed,
                    "ul_speed": ul_speed,
                    "dl_history": speed_history[iface]["dl"],
                    "ul_history": speed_history[iface]["ul"],
                    "bytes_sent": nc.bytes_sent,
                    "bytes_recv": nc.bytes_recv,
                    "packets_sent": nc.packets_sent,
                    "packets_recv": nc.packets_recv,
                    "errin": nc.errin,
                    "errout": nc.errout,
                    "dropin": nc.dropin,
                    "dropout": nc.dropout,
                }
        new_prev = {
            k: (v.bytes_sent, v.bytes_recv) for k, v in now_counters.items()
        }
        return result, new_prev, now_time
    except Exception:
        return {}, prev_counters, prev_time


def collect_wifi():
    try:
        result = subprocess.run(
            ["iwconfig", "wlan0"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        output = result.stdout + result.stderr

        ssid_match = re.search(r'ESSID:"(.+?)"', output)
        signal_match = re.search(r"Signal level=(-?\d+)", output)
        quality_match = re.search(r"Link Quality=(\d+)/(\d+)", output)
        freq_match = re.search(r"Frequency:([\d.]+ \w+)", output)
        bitrate_match = re.search(r"Bit Rate=([\d.]+ \w+/s)", output)

        ssid = ssid_match.group(1) if ssid_match else None
        signal_dbm = int(signal_match.group(1)) if signal_match else None
        quality_cur = int(quality_match.group(1)) if quality_match else 0
        quality_max = int(quality_match.group(2)) if quality_match else 100
        frequency = freq_match.group(1) if freq_match else "-"
        bitrate = bitrate_match.group(1) if bitrate_match else "-"

        # Derive channel from frequency
        channel = "-"
        if freq_match:
            try:
                ghz = float(re.search(r"([\d.]+)", frequency).group(1))
                if 2.4 <= ghz <= 2.5:
                    channel = str(round((ghz - 2.407) / 0.005))
                elif 5.0 <= ghz <= 5.9:
                    channel = str(round((ghz - 5.0) / 0.005))
            except Exception:
                pass

        return {
            "ssid": ssid,
            "signal_dbm": signal_dbm,
            "quality_cur": quality_cur,
            "quality_max": quality_max,
            "frequency": frequency,
            "bitrate": bitrate,
            "channel": channel,
        }
    except Exception:
        return {"ssid": None}


def collect_devices(dns_cache):
    try:
        result = subprocess.run(
            ["ip", "neigh", "show"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        devices = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            match = re.match(
                r"^(\S+)\s+dev\s+(\S+)\s+lladdr\s+(\S+)\s+(\S+)", line
            )
            if match:
                ip, iface, mac, state = match.groups()
                if ip in dns_cache:
                    hostname = dns_cache[ip]
                else:
                    try:
                        old_timeout = socket.getdefaulttimeout()
                        socket.setdefaulttimeout(0.3)
                        hostname = socket.gethostbyaddr(ip)[0]
                        socket.setdefaulttimeout(old_timeout)
                    except Exception:
                        hostname = ""
                        socket.setdefaulttimeout(None)
                    dns_cache[ip] = hostname
                devices.append(
                    {
                        "ip": ip,
                        "mac": mac,
                        "hostname": hostname,
                        "interface": iface,
                        "state": state,
                    }
                )
        # Sort by IP
        try:
            devices.sort(key=lambda d: socket.inet_aton(d["ip"]))
        except Exception:
            pass
        return devices
    except Exception:
        return []


def collect_connections():
    try:
        conns = psutil.net_connections(kind="inet")
        result = []
        for c in conns:
            if c.status in ("ESTABLISHED", "LISTEN") and c.laddr:
                if c.laddr.ip in ("127.0.0.1", "::1"):
                    continue
                remote_ip = c.raddr.ip if c.raddr else "-"
                remote_port = c.raddr.port if c.raddr else "-"
                # Get process name
                pname = ""
                if c.pid:
                    try:
                        pname = psutil.Process(c.pid).name()
                    except Exception:
                        pass
                result.append(
                    {
                        "proto": "TCP",
                        "local_port": c.laddr.port,
                        "remote_ip": remote_ip,
                        "remote_port": remote_port,
                        "status": c.status,
                        "process": pname,
                    }
                )
        # ESTABLISHED first, then LISTEN
        result.sort(key=lambda x: (0 if x["status"] == "ESTABLISHED" else 1))
        return result[:15]
    except Exception:
        return []


# ── Panel Renderers ──────────────────────────────────────────────────────────


def render_header(data):
    text = Text()
    text.append("  NETWORK DASHBOARD", style="bold bright_white")
    text.append("  |  ", style="dim")
    text.append(data["hostname"], style="bold cyan")
    text.append("  |  ", style="dim")
    text.append(f"up {data['uptime']}", style="green")
    text.append("  |  ", style="dim")
    text.append(data["date"], style="dim")
    text.append(" ", style="dim")
    text.append(data["time"], style="bold yellow")
    return Panel(
        Align.center(text),
        border_style="bright_blue",
        style="on dark_blue",
    )


def render_interfaces(data):
    table = Table(expand=True, show_header=True, header_style="bold")
    table.add_column("Interface", style="bold", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("IPv4", no_wrap=True)
    table.add_column("MAC", no_wrap=True)
    table.add_column("MTU", justify="right", no_wrap=True)

    for iface in data:
        status = (
            Text("UP", style="bold green")
            if iface["is_up"]
            else Text("DOWN", style="bold red")
        )
        table.add_row(
            iface["name"],
            status,
            iface["ipv4"],
            iface["mac"],
            str(iface["mtu"]),
        )

    return Panel(table, title="[bold]Interfaces[/]", border_style="cyan")


def render_bandwidth(data):
    content = Text()
    active = [iface for iface in INTERFACES if iface in data]
    if not active:
        content.append("No active interfaces", style="dim")
    for iface in active:
        d = data[iface]
        # Interface name
        content.append(f"  {iface}\n", style="bold")
        # Download
        content.append("    \u25bc ", style="green")
        content.append(f"{format_speed(d['dl_speed']):>12s}  ", style="bold green")
        content.append_text(make_sparkline(d["dl_history"], "green"))
        content.append("\n")
        # Upload
        content.append("    \u25b2 ", style="red")
        content.append(f"{format_speed(d['ul_speed']):>12s}  ", style="bold red")
        content.append_text(make_sparkline(d["ul_history"], "red"))
        content.append("\n")

    return Panel(content, title="[bold]Live Bandwidth[/]", border_style="green")


def render_wifi(data):
    if not data.get("ssid"):
        return Panel(
            Align.center(Text("WiFi not connected", style="dim")),
            title="[bold]WiFi[/]",
            border_style="dim",
        )

    content = Text()
    content.append("  SSID: ", style="dim")
    content.append(f"{data['ssid']}\n", style="bold bright_white")

    # Signal bar
    dbm = data.get("signal_dbm")
    qual_cur = data.get("quality_cur", 0)
    qual_max = data.get("quality_max", 100)
    pct = int((qual_cur / qual_max) * 100) if qual_max else 0
    bar_width = 20
    filled = int((qual_cur / qual_max) * bar_width) if qual_max else 0
    color = signal_color(dbm)
    content.append("  Signal: ", style="dim")
    if dbm is not None:
        content.append(f"{dbm} dBm ", style=f"bold {color}")
    content.append("[", style="dim")
    content.append("\u2588" * filled, style=color)
    content.append("\u2500" * (bar_width - filled), style="dim")
    content.append("]", style="dim")
    content.append(f" {pct}%\n", style=f"bold {color}")

    content.append(f"  Channel: ", style="dim")
    content.append(f"{data.get('channel', '-')}", style="bright_white")
    content.append(f"  |  {data.get('frequency', '-')}", style="dim")
    content.append(f"  |  {data.get('bitrate', '-')}\n", style="dim")

    return Panel(content, title="[bold]WiFi[/]", border_style="magenta")


def render_devices(data):
    table = Table(expand=True, show_header=True, header_style="bold")
    table.add_column("IP", no_wrap=True)
    table.add_column("MAC", no_wrap=True)
    table.add_column("Host", no_wrap=True, max_width=20)
    table.add_column("State", no_wrap=True)

    for dev in data:
        # Highlight gateway
        ip_style = "bold bright_white" if dev["ip"].endswith(".1") else ""
        state_style = (
            "green"
            if dev["state"] == "REACHABLE"
            else "dim yellow" if dev["state"] == "STALE" else "dim"
        )
        table.add_row(
            Text(dev["ip"], style=ip_style),
            dev["mac"][-8:],
            dev["hostname"] or "-",
            Text(dev["state"], style=state_style),
        )

    title = f"[bold]Devices ({len(data)})[/]"
    return Panel(table, title=title, border_style="yellow")


def render_connections(data):
    table = Table(expand=True, show_header=True, header_style="bold")
    table.add_column("Port", justify="right", no_wrap=True, width=6)
    table.add_column("Dir", no_wrap=True, width=3)
    table.add_column("Remote", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Process", no_wrap=True, max_width=12)

    for conn in data:
        if conn["status"] == "ESTABLISHED":
            status_style = "green"
            direction = "<->" if conn["remote_ip"] != "-" else ""
        elif conn["status"] == "LISTEN":
            status_style = "cyan"
            direction = " * "
        else:
            status_style = "dim"
            direction = ""

        remote = (
            f"{conn['remote_ip']}:{conn['remote_port']}"
            if conn["remote_ip"] != "-"
            else "-"
        )
        # Truncate remote if too long
        if len(remote) > 22:
            remote = remote[:20] + ".."

        table.add_row(
            str(conn["local_port"]),
            Text(direction, style="dim"),
            remote,
            Text(conn["status"], style=status_style),
            conn["process"],
        )

    return Panel(table, title="[bold]Connections[/]", border_style="red")


def render_netstats(data):
    content = Text()
    active = [iface for iface in INTERFACES if iface in data]
    if not active:
        content.append("No data", style="dim")

    for iface in active:
        d = data[iface]
        content.append(f"  {iface}\n", style="bold")
        content.append("    TX: ", style="dim")
        content.append(f"{format_bytes(d['bytes_sent']):>10s}", style="red")
        content.append(f"  ({d['packets_sent']:,} pkts)\n", style="dim")
        content.append("    RX: ", style="dim")
        content.append(f"{format_bytes(d['bytes_recv']):>10s}", style="green")
        content.append(f"  ({d['packets_recv']:,} pkts)\n", style="dim")
        content.append("    Errors: ", style="dim")
        err_style = "bold red" if (d["errin"] + d["errout"]) > 0 else "green"
        content.append(f"{d['errin']}/{d['errout']}", style=err_style)
        content.append("  Drops: ", style="dim")
        drop_style = "bold yellow" if (d["dropin"] + d["dropout"]) > 0 else "green"
        content.append(f"{d['dropin']}/{d['dropout']}\n", style=drop_style)

    return Panel(content, title="[bold]Network Stats[/]", border_style="blue")


# ── Layout ───────────────────────────────────────────────────────────────────


def build_layout():
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="upper", size=8),
        Layout(name="middle", ratio=1),
        Layout(name="lower", ratio=1),
    )
    layout["upper"].split_row(
        Layout(name="interfaces", ratio=1),
        Layout(name="wifi", ratio=1),
    )
    layout["middle"].split_row(
        Layout(name="bandwidth", ratio=3),
        Layout(name="netstats", ratio=2),
    )
    layout["lower"].split_row(
        Layout(name="devices", ratio=1),
        Layout(name="connections", ratio=1),
    )
    return layout


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    console = Console()
    layout = build_layout()

    # State
    prev_counters = {}
    prev_time = time.monotonic()
    speed_history = {
        iface: {
            "dl": collections.deque(maxlen=SPARKLINE_WIDTH),
            "ul": collections.deque(maxlen=SPARKLINE_WIDTH),
        }
        for iface in INTERFACES
    }
    dns_cache = {}
    cache = {}
    tick = 0

    # Seed initial counters so first tick has a baseline
    initial = psutil.net_io_counters(pernic=True)
    prev_counters = {k: (v.bytes_sent, v.bytes_recv) for k, v in initial.items()}
    time.sleep(0.1)

    with Live(
        layout,
        console=console,
        refresh_per_second=2,
        screen=True,
        vertical_overflow="crop",
    ):
        while True:
            # ── Collect ──
            cache["header"] = collect_header()

            bw_data, prev_counters, prev_time = collect_bandwidth(
                prev_counters, prev_time, speed_history
            )
            cache["bandwidth"] = bw_data

            if tick % 2 == 0:
                cache["connections"] = collect_connections()

            if tick % 5 == 0:
                cache["interfaces"] = collect_interfaces()
                cache["wifi"] = collect_wifi()

            if tick % 15 == 0:
                cache["devices"] = collect_devices(dns_cache)

            # ── Render ──
            layout["header"].update(render_header(cache["header"]))

            if "interfaces" in cache:
                layout["interfaces"].update(render_interfaces(cache["interfaces"]))
            if "wifi" in cache:
                layout["wifi"].update(render_wifi(cache["wifi"]))

            layout["bandwidth"].update(render_bandwidth(cache.get("bandwidth", {})))
            layout["netstats"].update(render_netstats(cache.get("bandwidth", {})))

            if "devices" in cache:
                layout["devices"].update(render_devices(cache["devices"]))
            if "connections" in cache:
                layout["connections"].update(render_connections(cache["connections"]))

            time.sleep(TICK_INTERVAL)
            tick += 1


if __name__ == "__main__":
    main()
