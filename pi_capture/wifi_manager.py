#!/usr/bin/env python3
"""
BuzzOff WiFi Manager  (runs as buzzoff-wifi.service, as root)

What it does
------------
1. Normally does nothing: the Pi joins any saved WiFi on its own
   (NetworkManager picks the highest-priority saved network in range).
2. If the Pi has NO WiFi connection for GRACE_SECONDS, it turns on its own
   hotspot called "BuzzOff-Setup".
3. Connect your phone/laptop to "BuzzOff-Setup". A setup page pops up
   (or open http://10.42.0.1). Pick a network, type its password, tap Save.
4. The Pi saves that network permanently, turns the hotspot off, and joins it.
   If joining fails, the hotspot comes back and the page shows the error.
5. While the hotspot is on and nobody is using the page, every
   RETRY_SECONDS the Pi briefly turns the hotspot off and tries its saved
   networks again (handles "the WiFi came back").

The setup page only answers devices on the setup hotspot (10.42.0.x) or on
your Tailscale network (100.64.0.0/10), so strangers on a public WiFi cannot
reach it.

Requires: NetworkManager (default on Raspberry Pi OS Bookworm and later),
          python3-flask  (sudo apt install -y python3-flask)
"""

import html
import ipaddress
import os
import re
import subprocess
import threading
import time
from datetime import datetime

from flask import Flask, request, redirect

# ==================== CONFIGURATION ====================
IFACE = "wlan0"
AP_CON_NAME = "BuzzOff-Setup-AP"        # NetworkManager profile name for the hotspot
AP_SSID = "BuzzOff-Setup"               # hotspot name you will see on your phone
AP_PASSWORD_FILE = "/etc/buzzoff/ap_password"   # 8+ characters, one line
AP_PASSWORD_DEFAULT = "buzzoff-setup"   # used only if the file is missing
SAVED_PREFIX = "bz-"                     # profile names for networks saved via this page

BOOT_WAIT_SECONDS = 45      # give saved networks time to connect after boot
GRACE_SECONDS = 60          # offline this long -> start setup hotspot
RETRY_SECONDS = 300         # in hotspot mode, retry saved networks this often
RETRY_TRY_SECONDS = 40      # how long each retry waits for a saved network
PAGE_IDLE_SECONDS = 120     # don't retry if someone used the page this recently
LOOP_SECONDS = 10
WEB_PORT = 80

ALLOWED_NETS = [ipaddress.ip_network("10.42.0.0/24"),
                ipaddress.ip_network("100.64.0.0/10"),
                ipaddress.ip_network("127.0.0.0/8")]

# ==================== STATE ====================
lock = threading.RLock()
state = {
    "mode": "starting",          # starting | online | offline | hotspot | joining
    "last_scan": [],             # [(ssid, signal, security)]
    "last_message": "",
    "last_page_hit": 0.0,
}


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} [wifi] {msg}", flush=True)


# ==================== NMCLI HELPERS ====================
def nm(*args, timeout=30):
    """Run nmcli and return (ok, stdout+stderr)."""
    try:
        r = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, "nmcli timed out"


def split_terse(line):
    """nmcli -t output: fields separated by ':' with '\\:' escaping."""
    parts = re.split(r"(?<!\\):", line)
    return [p.replace("\\:", ":").replace("\\\\", "\\") for p in parts]


def wifi_status():
    """Return (state, connection_name) for IFACE."""
    ok, out = nm("-t", "-f", "DEVICE,STATE,CONNECTION", "device")
    if not ok:
        return "unknown", ""
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) >= 3 and f[0] == IFACE:
            return f[1], f[2]
    return "missing", ""


def is_client_connected():
    st, con = wifi_status()
    return st.startswith("connected") and con != AP_CON_NAME


def is_ap_active():
    st, con = wifi_status()
    return con == AP_CON_NAME


def scan_networks():
    """Scan for nearby WiFi. Only reliable while NOT in hotspot mode."""
    ok, out = nm("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                 "ifname", IFACE, "--rescan", "yes", timeout=30)
    nets = {}
    if ok:
        for line in out.splitlines():
            f = split_terse(line)
            if len(f) < 3 or not f[0] or f[0] == AP_SSID:
                continue
            ssid, signal, sec = f[0], int(f[1] or 0), f[2]
            if ssid not in nets or signal > nets[ssid][1]:
                nets[ssid] = (ssid, signal, sec)
    result = sorted(nets.values(), key=lambda n: -n[1])
    with lock:
        state["last_scan"] = result
    log(f"scan found {len(result)} networks")
    return result


def saved_networks():
    """List saved WiFi profiles: [(name, ssid, priority)]."""
    ok, out = nm("-t", "-f", "NAME,TYPE", "connection", "show")
    saved = []
    if not ok:
        return saved
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) >= 2 and f[1] == "802-11-wireless" and f[0] != AP_CON_NAME:
            ok2, detail = nm("-t", "-g", "802-11-wireless.ssid,connection.autoconnect-priority",
                             "connection", "show", f[0])
            vals = detail.splitlines() if ok2 else ["?", "0"]
            ssid = vals[0] if vals else "?"
            prio = vals[1] if len(vals) > 1 else "0"
            saved.append((f[0], ssid, prio))
    return sorted(saved, key=lambda s: -int(s[2] or 0))


def read_ap_password():
    try:
        with open(AP_PASSWORD_FILE) as f:
            pw = f.read().strip()
        if len(pw) >= 8:
            return pw
        log("AP password file has fewer than 8 characters; using default")
    except FileNotFoundError:
        log(f"{AP_PASSWORD_FILE} missing; using default hotspot password")
    return AP_PASSWORD_DEFAULT


def start_ap():
    with lock:
        scan_networks()   # scan first: scanning is unreliable once the hotspot is up
        nm("connection", "delete", AP_CON_NAME)
        ok, out = nm("connection", "add", "type", "wifi", "ifname", IFACE,
                     "con-name", AP_CON_NAME, "autoconnect", "no", "ssid", AP_SSID,
                     "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
                     "ipv4.method", "shared", "ipv6.method", "disabled",
                     "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", read_ap_password())
        if not ok:
            log(f"could not create hotspot profile: {out}")
            return False
        ok, out = nm("connection", "up", AP_CON_NAME, timeout=40)
        if ok:
            state["mode"] = "hotspot"
            log(f"hotspot '{AP_SSID}' is ON at http://10.42.0.1")
        else:
            log(f"could not start hotspot: {out}")
        return ok


def stop_ap():
    with lock:
        nm("connection", "down", AP_CON_NAME, timeout=20)
        log("hotspot OFF")


def wait_for_client(seconds):
    end = time.time() + seconds
    while time.time() < end:
        if is_client_connected():
            return True
        time.sleep(3)
    return False


def save_network(ssid, password, priority):
    """Save a WiFi profile permanently. Returns (ok, message)."""
    con_name = SAVED_PREFIX + ssid
    nm("connection", "delete", con_name)
    args = ["connection", "add", "type", "wifi", "ifname", IFACE, "con-name", con_name,
            "ssid", ssid, "connection.autoconnect", "yes",
            "connection.autoconnect-priority", str(priority)]
    if password:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    ok, out = nm(*args)
    return ok, (f"Saved '{ssid}'" if ok else f"Could not save '{ssid}': {out}")


def join_network(ssid, password, priority):
    """Save the network, drop the hotspot, try to join. Runs in a thread."""
    with lock:
        state["mode"] = "joining"
        ok, msg = save_network(ssid, password, priority)
        if not ok:
            state["last_message"] = msg
            state["mode"] = "hotspot"
            return
        time.sleep(3)   # let the phone receive the "joining" page first
        stop_ap()
        ok, out = nm("connection", "up", SAVED_PREFIX + ssid, timeout=45)
        if ok and wait_for_client(10):
            state["last_message"] = f"Connected to '{ssid}'. It is saved for next time."
            state["mode"] = "online"
            log(f"joined '{ssid}'")
        else:
            state["last_message"] = (f"Saved '{ssid}' but could not connect "
                                     f"(wrong password, out of range, or a sign-in-page WiFi). "
                                     f"Details: {out[-200:]}")
            log(f"join failed for '{ssid}': {out}")
            start_ap()


def forget_network(con_name):
    ok, out = nm("connection", "delete", con_name)
    return f"Forgot '{con_name}'" if ok else f"Could not forget: {out}"


# ==================== WEB PAGE ====================
app = Flask(__name__)

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BuzzOff WiFi Setup</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:520px;margin:0 auto;padding:16px;color:#1b1b1b;background:#fafafa}}
h1{{font-size:22px}} h2{{font-size:17px;margin-top:28px}}
.card{{background:#fff;border:1px solid #ddd;border-radius:10px;padding:14px;margin:10px 0}}
label{{display:block;margin:10px 0 4px;font-weight:600}}
select,input{{width:100%;box-sizing:border-box;padding:10px;font-size:16px;border:1px solid #bbb;border-radius:8px}}
button{{width:100%;padding:12px;font-size:16px;border:0;border-radius:8px;background:#1f6feb;color:#fff;margin-top:14px}}
.msg{{background:#fff7d6;border:1px solid #e6c200;border-radius:8px;padding:10px}}
.small{{color:#666;font-size:13px}} table{{width:100%;border-collapse:collapse}}
td{{padding:6px 4px;border-bottom:1px solid #eee;font-size:14px}}
.forget{{background:#c62828;padding:6px 10px;width:auto;margin:0;font-size:13px}}
</style></head><body>
<h1>BuzzOff WiFi Setup</h1>
<p class="small">Device status: <b>{mode}</b> &middot; current WiFi: <b>{current}</b></p>
{message}
<div class="card"><form method="post" action="/save">
<label>Network</label>
<select name="ssid_pick">{options}<option value="__other__">Other (type name below)</option></select>
<label>Network name (only if \"Other\" or hidden)</label>
<input name="ssid_typed" autocomplete="off" autocapitalize="off">
<label>Password (leave blank for open WiFi)</label>
<input name="password" type="password" autocomplete="off">
<label>Priority</label>
<select name="priority">
<option value="100">Highest &ndash; my phone hotspot (always try first)</option>
<option value="50" selected>Normal &ndash; home / school / field site</option>
<option value="10">Low &ndash; public WiFi</option></select>
<button type="submit">Save and connect</button>
<p class="small">After you tap Save, this page will disconnect. Wait 1 minute.
If the Pi connects, the BuzzOff-Setup hotspot disappears and the Pi is online.
If it fails, BuzzOff-Setup comes back &mdash; reconnect and you will see why.</p>
</form></div>
<h2>Saved networks</h2><div class="card"><table>{saved}</table></div>
<p class="small"><a href="/rescan">Rescan networks</a> (briefly drops this hotspot)</p>
</body></html>"""


def client_allowed():
    try:
        ip = ipaddress.ip_address(request.remote_addr)
        return any(ip in n for n in ALLOWED_NETS)
    except ValueError:
        return False


@app.before_request
def guard():
    if not client_allowed():
        return "Not available on this network.", 403
    with lock:
        state["last_page_hit"] = time.time()


def render():
    with lock:
        scan = list(state["last_scan"])
        message = state["last_message"]
        mode = state["mode"]
    st, con = wifi_status()
    current = "setup hotspot" if con == AP_CON_NAME else (con or "none")
    options = "".join(
        f'<option value="{html.escape(s)}">{html.escape(s)} ({sig}%{", open" if not sec else ""})</option>'
        for s, sig, sec in scan) or '<option value="__other__">No scan results yet</option>'
    rows = "".join(
        f'<tr><td>{html.escape(ssid)}</td><td>priority {html.escape(p)}</td>'
        f'<td><form method="post" action="/forget" style="margin:0">'
        f'<input type="hidden" name="con" value="{html.escape(name)}">'
        f'<button class="forget">Forget</button></form></td></tr>'
        for name, ssid, p in saved_networks()) or "<tr><td>None saved yet</td></tr>"
    msg_html = f'<p class="msg">{html.escape(message)}</p>' if message else ""
    return PAGE.format(mode=html.escape(mode), current=html.escape(current),
                       message=msg_html, options=options, saved=rows)


@app.route("/save", methods=["POST"])
def save():
    ssid = request.form.get("ssid_pick", "")
    if ssid == "__other__" or not ssid:
        ssid = request.form.get("ssid_typed", "").strip()
    password = request.form.get("password", "")
    try:
        priority = int(request.form.get("priority", "50"))
    except ValueError:
        priority = 50
    if not ssid:
        state["last_message"] = "Please pick or type a network name."
        return redirect("/")
    if password and len(password) < 8:
        state["last_message"] = "WiFi passwords are at least 8 characters. Please check it."
        return redirect("/")

    if is_ap_active():
        # Must drop the hotspot to join; do it in the background so this page can answer first.
        threading.Thread(target=join_network, args=(ssid, password, priority), daemon=True).start()
        return (f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<p style='font-family:system-ui;padding:16px'>Joining <b>{html.escape(ssid)}</b>&hellip; "
                f"This page will disconnect. If BuzzOff-Setup reappears within 2 minutes, "
                f"reconnect to it to see what went wrong.</p>")
    # Already online (e.g. reached over Tailscale): just save it for later.
    ok, msg = save_network(ssid, password, priority)
    state["last_message"] = msg + (" It will be used automatically when in range." if ok else "")
    return redirect("/")


@app.route("/forget", methods=["POST"])
def forget():
    con = request.form.get("con", "")
    if con:
        state["last_message"] = forget_network(con)
    return redirect("/")


@app.route("/rescan")
def rescan():
    if is_ap_active():
        def _rescan():
            stop_ap()
            if not wait_for_client(RETRY_TRY_SECONDS):
                start_ap()
        threading.Thread(target=_rescan, daemon=True).start()
        return ("<p style='font-family:system-ui;padding:16px'>Rescanning. The hotspot will come back "
                "in about a minute (unless a saved network connects). Then reconnect and reload.</p>")
    scan_networks()
    return redirect("/")


# Captive-portal catch-all: phones probing for internet get the setup page,
# which makes iPhone/Android pop it up automatically.
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def index(path):
    return render()


# ==================== MAIN LOOP ====================
def monitor():
    log(f"waiting {BOOT_WAIT_SECONDS}s for saved networks after boot")
    time.sleep(BOOT_WAIT_SECONDS)
    offline_since = None
    hotspot_since = None
    while True:
        try:
            with lock:
                mode = state["mode"]
            if mode == "joining":
                pass
            elif is_client_connected():
                if state["mode"] != "online":
                    log(f"online via '{wifi_status()[1]}'")
                state["mode"] = "online"
                offline_since = hotspot_since = None
            elif is_ap_active():
                state["mode"] = "hotspot"
                hotspot_since = hotspot_since or time.time()
                idle = time.time() - state["last_page_hit"] > PAGE_IDLE_SECONDS
                if idle and time.time() - hotspot_since > RETRY_SECONDS:
                    log("retrying saved networks")
                    stop_ap()
                    if not wait_for_client(RETRY_TRY_SECONDS):
                        start_ap()
                    hotspot_since = time.time()
            else:
                state["mode"] = "offline"
                offline_since = offline_since or time.time()
                if time.time() - offline_since > GRACE_SECONDS:
                    log(f"offline for {GRACE_SECONDS}s, starting setup hotspot")
                    if start_ap():
                        hotspot_since = time.time()
                    offline_since = None
        except Exception as e:   # never let the monitor die
            log(f"monitor error: {e}")
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("Run as root (the service does this). For a manual test: sudo python3 wifi_manager.py")
        raise SystemExit(1)
    threading.Thread(target=monitor, daemon=True).start()
    app.run(host="0.0.0.0", port=WEB_PORT, threaded=True)
