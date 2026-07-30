#!/usr/bin/env python3
import json
import re
import http.cookiejar
import urllib.request
from pathlib import Path

token = None
for p in [
    Path("/opt/tsingpaw/data/config.json"),
    Path("/etc/tsingpaw.conf"),
]:
    if not p.exists():
        continue
    text = p.read_text(errors="ignore")
    for pat in [
        r'(?i)"token"\s*:\s*"([^"]+)"',
        r"(?i)DASHBOARD_TOKEN=(\S+)",
        r"(?i)TOKEN=(\S+)",
    ]:
        m = re.search(pat, text)
        if m and len(m.group(1)) >= 8 and "token" not in m.group(1).lower():
            # avoid pico channel token if possible - look for launcher auth
            pass

# Launcher auth token is usually separate; try auth status without login first
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def get(url):
    req = urllib.request.Request(url)
    with opener.open(req, timeout=5) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


# find token from launcher process env or known file
candidates = []
for p in Path("/opt/tsingpaw").rglob("*"):
    if p.is_file() and p.stat().st_size < 200000 and "token" in p.name.lower():
        candidates.append(str(p))
print("token_files", candidates[:10])

# Try reading from factory / runtime without dumping secrets
cfg = json.loads(Path("/opt/tsingpaw/data/config.json").read_text())
# Often dashboard token is in gateway or top-level
keys = list(cfg.keys())
print("cfg_top_keys", keys)
for k in keys:
    if "token" in k.lower() or "auth" in k.lower() or "dash" in k.lower():
        v = cfg[k]
        print("cfg_key", k, "type", type(v).__name__, "len", len(str(v)) if not isinstance(v, (dict, list)) else "-")

# Also check /proc for launcher cmdline flags
import subprocess
out = subprocess.check_output(["ps", "w"], text=True)
for line in out.splitlines():
    if "picoclaw-launcher" in line:
        # redact anything after -token or similar
        safe = re.sub(r"(?i)(token|password|secret)\S*", r"\1***", line)
        print("proc", safe[:200])

st, ct, body = get("http://127.0.0.1:18800/")
html = body.decode("utf-8", "replace")
print("index_status", st, "ctype", ct)
print("has_css", "cloud-channel.css" in html)
print("has_js", "cloud-channel.js" in html)
print("inject_snip", re.findall(r"tsingpaws-cloud/[^\"']+", html))
print("body_tail", html[-300:].replace("\n", " "))

st, ct, body = get("http://127.0.0.1:18800/api/auth/status")
print("auth_status", st, body[:120])

st, ct, body = get("http://127.0.0.1:18800/api/channels/catalog")
print("catalog_status", st, body[:200])

st, ct, body = get("http://127.0.0.1:18800/tsingpaws-cloud/cloud-channel.js?v=6")
print("js_status", st, "len", len(body), "has_showPanel", b"showPanel" in body)
