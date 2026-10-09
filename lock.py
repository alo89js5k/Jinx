# Rebuilds the Xray config on every start from the repo files.
# The panel's Core Settings are read-only, so the repo is the single source of truth:
# no stale config from an old deploy, no corrupt JSON, and the locked inbound is always first.
# ⚡ Fast transports are added ONLY when this image's Xray AND Marzban both support them,
#    so a newer feature can never stop the core (all WS configs keep working no matter what).
import json, os, re, shutil, subprocess, time
DST = "/var/lib/marzban/xray_config.json"
base = json.load(open("/opt/xray_config.json"))
locked = json.load(open("/opt/locked_inbound.json"))
if os.path.exists(DST):
    try:
        json.load(open(DST))
    except Exception:
        shutil.copy(DST, DST + f".broken-{int(time.time())}")
        print("[lock] old config was corrupt, backed up and replaced", flush=True)

def xray_version():
    exe = os.environ.get("XRAY_EXECUTABLE_PATH") or shutil.which("xray") or "/usr/local/bin/xray"
    try:
        out = subprocess.run([exe, "version"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"Xray (\d+)\.(\d+)\.(\d+)", out)
        return tuple(int(v) for v in m.groups()) if m else (0, 0, 0)
    except Exception:
        return (0, 0, 0)

def marzban_knows(word):
    for root, _, files in os.walk("/code/app"):
        for f in files:
            if f.endswith(".py"):
                try:
                    if word in open(os.path.join(root, f), encoding="utf-8", errors="ignore").read():
                        return True
                except Exception:
                    pass
    return False

def inbound(tag, port, net, settings):
    return {"tag": tag, "listen": "127.0.0.1", "port": port, "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": dict({"network": net, "security": "none",
                                    "sockopt": {"tcpKeepAliveIdle": 30, "tcpKeepAliveInterval": 15}}, **settings),
            "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}}

ver = xray_version()
extra = []
if os.environ.get("JINX_FAST", "1") != "0":
    # HTTPUpgrade: same path as WS through Railway/Cloudflare, but lighter (no WS framing) -> faster
    if ver >= (1, 8, 9) and marzban_knows("httpupgrade"):
        extra.append(inbound("VLESS HTTPUPGRADE", 2005, "httpupgrade", {"httpupgradeSettings": {"path": "/vhu"}}))
    # XHTTP (or the older SplitHTTP): plain HTTP requests, passes any CDN, very stable on strict operators
    if ver >= (24, 11, 0) and marzban_knows("xhttp"):
        extra.append(inbound("VLESS XHTTP", 2006, "xhttp", {"xhttpSettings": {"path": "/vxh", "mode": "auto"}}))
    elif ver >= (1, 8, 16) and marzban_knows("splithttp"):
        extra.append(inbound("VLESS XHTTP", 2006, "splithttp", {"splithttpSettings": {"path": "/vxh"}}))

# 🛰 VLESS REALITY on a Railway TCP Proxy (optional): raw TCP, no web edge in between -> the lowest overhead.
#    Only when you turn on Settings > Networking > TCP Proxy for this service (Railway then sets these variables).
REALITY_TAG = "VLESS REALITY"
def reality_keys():
    kf = "/var/lib/marzban/jinx_reality.json"
    try:
        k = json.load(open(kf))
        if k.get("private") and k.get("sid"):
            return k
    except Exception:
        pass
    exe = os.environ.get("XRAY_EXECUTABLE_PATH") or shutil.which("xray") or "/usr/local/bin/xray"
    out = subprocess.run([exe, "x25519"], capture_output=True, text=True, timeout=10).stdout
    m = re.search(r"Private\s*key:\s*(\S+)", out, re.I)
    if not m:
        raise RuntimeError("xray x25519 gave no key")
    k = {"private": m.group(1), "sid": os.urandom(4).hex()}
    tmpk = kf + ".tmp"; json.dump(k, open(tmpk, "w")); os.chmod(tmpk, 0o600); os.replace(tmpk, kf)   # same key forever = configs never break
    return k
try:
    _rp = int(os.environ.get("RAILWAY_TCP_APPLICATION_PORT") or 0)
except ValueError:
    _rp = 0
if _rp and os.environ.get("JINX_REALITY", "1") != "0":
    if _rp in (8080, 8000, 8090, 2001, 2002, 2003, 2004, 2005, 2006) or not 1024 <= _rp <= 65535:
        print(f"[lock] TCP Proxy port {_rp} is already used inside -> set the TCP Proxy port to 2010 (Reality skipped)", flush=True)
    elif ver >= (1, 8, 0) and marzban_knows("reality"):
        try:
            k = reality_keys(); sni = (os.environ.get("JINX_REALITY_SNI") or "www.speedtest.net").strip()
            extra.append({"tag": REALITY_TAG, "listen": "0.0.0.0", "port": _rp, "protocol": "vless",
                          "settings": {"clients": [], "decryption": "none"},
                          "streamSettings": {"network": "tcp", "security": "reality",
                                             "realitySettings": {"show": False, "dest": sni + ":443", "xver": 0, "serverNames": [sni],
                                                                 "privateKey": k["private"], "shortIds": [k["sid"]]},
                                             "sockopt": {"tcpFastOpen": True, "tcpKeepAliveIdle": 30, "tcpKeepAliveInterval": 15}},
                          "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}})
            print(f"[lock] Reality on TCP Proxy port {_rp} (sni {sni})", flush=True)
        except Exception as e:
            print(f"[lock] Reality skipped: {e}", flush=True)

# 🚀 BBR: much steadier speed on lossy mobile networks. Turned on ONLY if this server's kernel allows it for us
#    (otherwise Xray would refuse to connect, so we never guess).
def bbr_ok():
    try:
        allowed = open("/proc/sys/net/ipv4/tcp_allowed_congestion_control").read().split()
        avail = open("/proc/sys/net/ipv4/tcp_available_congestion_control").read().split()
        return "bbr" in allowed and "bbr" in avail and os.environ.get("JINX_BBR", "1") != "0"
    except Exception:
        return False
BBR = bbr_ok()
if BBR:
    for o in base.get("outbounds", []):
        if o.get("protocol") == "freedom":
            o.setdefault("streamSettings", {}).setdefault("sockopt", {})["tcpcongestion"] = "bbr"
    for i in extra:
        if i.get("tag") == "VLESS REALITY":
            i["streamSettings"]["sockopt"]["tcpcongestion"] = "bbr"
print(f"[lock] BBR {'on' if BBR else 'not allowed by this server, normal TCP'}", flush=True)

# 💓 WS heartbeat: a tiny ping every 30 s keeps the phone's line open (operators cut idle lines),
#    so the app never has to reconnect = no ping jump after a pause. Old Xray simply ignores it.
_hb = os.environ.get("JINX_HEARTBEAT", "30").strip()
HB = int(_hb) if _hb.isdigit() else 30          # JINX_HEARTBEAT=0 turns it off
for i in [locked] + base.get("inbounds", []) + extra:
    ss = i.get("streamSettings") or {}
    if ss.get("network") == "ws" and isinstance(ss.get("wsSettings"), dict):
        if HB > 0:
            ss["wsSettings"]["heartbeatPeriod"] = HB
        else:
            ss["wsSettings"].pop("heartbeatPeriod", None)

base["inbounds"] = [locked] + [i for i in base["inbounds"]
                               if i["tag"] != locked["tag"] and i["port"] != locked["port"]] + extra
_seen_t, _seen_p, _keep = set(), set(), []
for i in base["inbounds"]:                       # a duplicate tag/port would stop Xray -> keep the first, drop the copy
    if i["tag"] in _seen_t or i["port"] in _seen_p:
        print(f"[lock] WARNING: duplicate inbound {i['tag']} / {i['port']} skipped", flush=True); continue
    _seen_t.add(i["tag"]); _seen_p.add(i["port"]); _keep.append(i)
# 🚫 ad blocker: ads and trackers of sites and apps are dropped on the server (less data, pages open faster).
#    Turned on only if this image's geosite file really has the ads list (a missing list would stop Xray).
def geosite_has(word):
    dirs = [os.environ.get("XRAY_ASSETS_PATH") or "", "/usr/local/share/xray", "/usr/share/xray", "/usr/local/lib/xray",
            os.path.dirname(os.environ.get("XRAY_EXECUTABLE_PATH") or "/usr/local/bin/xray")]
    for d in dirs:
        f = os.path.join(d, "geosite.dat") if d else ""
        if f and os.path.exists(f):
            try:
                with open(f, "rb") as fh:
                    return word.upper().encode() in fh.read()
            except Exception:
                return False
    return False
if os.environ.get("JINX_ADBLOCK", "1") != "0" and geosite_has("category-ads-all"):
    rules = base.setdefault("routing", {}).setdefault("rules", [])
    rule = {"type": "field", "domain": ["geosite:category-ads-all"], "outboundTag": "BLOCK"}
    if rule not in rules:
        rules.insert(0, rule)
    print("[lock] ad blocker on (geosite:category-ads-all)", flush=True)
else:
    print("[lock] ad blocker off (no ads list in this image, or JINX_ADBLOCK=0)", flush=True)

# order = order in the subscription: lightest / steadiest first (the locked inbound always stays first)
_RANK = {locked["tag"]: 0, "VLESS REALITY": 1, "VLESS HTTPUPGRADE": 2, "VLESS XHTTP": 3, "VLESS WS": 4, "TROJAN WS": 5, "VMESS WS": 6}
base["inbounds"] = sorted(_keep, key=lambda i: _RANK.get(i["tag"], 50))
tags = [i["tag"] for i in base["inbounds"]]
tmp = DST + ".tmp"
json.dump(base, open(tmp, "w"), indent=2, ensure_ascii=False)
os.replace(tmp, DST)  # atomic write
print(f"[lock] xray {'.'.join(map(str, ver))} | config ready: {len(tags)} inbounds "
      f"({', '.join(tags)}) | locked = {locked['tag']}", flush=True)
