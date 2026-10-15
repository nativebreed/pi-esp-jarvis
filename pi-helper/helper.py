#!/usr/bin/env python3
"""Jarvis Pi helper: lets the assistant container do a fixed list of things on the Pi.

It only runs the commands listed in actions.json, only answers requests that come
from this machine or its Docker containers, and every request needs the secret token.
Standard library only, no installs needed.
"""
import ipaddress
import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
ACTIONS_FILE = Path(os.environ.get("JARVIS_ACTIONS", HERE / "actions.json"))
TOKEN_FILE = Path(os.environ.get("JARVIS_TOKEN_FILE", Path.home() / ".config/jarvis-helper/token"))
PORT = int(os.environ.get("JARVIS_HELPER_PORT", "8766"))
ALLOWED_NETS = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "172.16.0.0/12", "::1/128")]


def load_actions() -> dict:
    return json.loads(ACTIONS_FILE.read_text())


def token() -> str:
    return TOKEN_FILE.read_text().strip()


# ----------------------------------------------------------------- status
def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def _cpu_percent() -> float | None:
    def snap():
        parts = _read("/proc/stat").splitlines()[0].split()[1:]
        vals = list(map(int, parts))
        return vals[3] + vals[4], sum(vals)
    try:
        i1, t1 = snap()
        time.sleep(0.3)
        i2, t2 = snap()
        return 100.0 * (1 - (i2 - i1) / max(1, t2 - t1))
    except Exception:
        return None


def status() -> dict:
    out: dict = {}
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    out["temperature"] = int(temp) / 1000 if temp.isdigit() else None
    out["cpu_percent"] = _cpu_percent()
    try:
        mem = {l.split(":")[0]: int(l.split()[1]) for l in _read("/proc/meminfo").splitlines()}
        out["memory_percent"] = 100 * (1 - mem["MemAvailable"] / mem["MemTotal"])
    except Exception:
        out["memory_percent"] = None
    du = shutil.disk_usage("/")
    out["disk_percent"] = 100 * du.used / du.total
    up = _read("/proc/uptime")
    out["uptime_s"] = float(up.split()[0]) if up else None
    out["load"] = _read("/proc/loadavg").split()[:3]
    if shutil.which("vcgencmd"):
        try:
            r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3)
            val = int(r.stdout.strip().split("=")[1], 16)
            out["throttled"] = bool(val & 0xF)   # happening right now
        except Exception:
            pass
    if shutil.which("docker"):
        try:
            r = subprocess.run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
                               capture_output=True, text=True, timeout=5)
            out["containers"] = [
                {"name": n, "status": s, "healthy": "unhealthy" not in s and s.startswith("Up")}
                for n, s in (l.split("\t", 1) for l in r.stdout.strip().splitlines() if "\t" in l)]
        except Exception:
            pass
    return out


# ----------------------------------------------------------------- running things
def _spawn(cmd: list[str], background: bool, delay: float = 0) -> dict:
    def go():
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    if delay or background:
        if delay:
            threading.Timer(delay, go).start()
        else:
            go()
        return {"ok": True}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return {"ok": r.returncode == 0, "output": (r.stdout or r.stderr)[-500:],
            "error": "" if r.returncode == 0 else (r.stderr or r.stdout)[-200:]}


def _browser(url: str) -> list[str]:
    cfg = load_actions()
    if cfg.get("browser_command"):
        return [c.replace("{url}", url) for c in cfg["browser_command"]]
    for b in ("chromium-browser", "chromium", "firefox"):
        if shutil.which(b):
            return [b, "--new-window", url] if "chrom" in b else [b, url]
    return ["xdg-open", url]


def run(name: str, arg: str) -> dict:
    cfg = load_actions()
    try:
        if name == "open_url":
            u = urlparse(arg)
            if u.scheme not in ("http", "https") or not u.netloc:
                return {"ok": False, "error": "Only web addresses can be opened."}
            return _spawn(_browser(arg), background=True)
        if name in ("open_app", "close_app"):
            app = cfg.get("apps", {}).get(arg)
            if not app:
                return {"ok": False, "error": f"I don't know an app called {arg}."}
            if name == "open_app":
                if not shutil.which(app["command"][0]):
                    return {"ok": False, "error": f"{arg} isn't installed."}
                return _spawn(app["command"], background=True)
            proc = app.get("process") or Path(app["command"][0]).name
            subprocess.run(["pkill", "-f", proc], timeout=5)
            return {"ok": True}
        for act in cfg.get("actions", []):
            if act["name"] == name:
                if not act.get("enabled", True):
                    return {"ok": False, "error": "That action is switched off in actions.json."}
                return _spawn(act["command"], bool(act.get("background")), float(act.get("delay", 0)))
        return {"ok": False, "error": f"Unknown action {name}."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "It took too long."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


# ----------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def _allowed(self) -> bool:
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        if not any(ip in n for n in ALLOWED_NETS):
            return False
        return self.headers.get("X-Token", "") == token()

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        if self.path == "/status":
            return self._send(200, status())
        if self.path == "/actions":
            cfg = load_actions()
            return self._send(200, {
                "apps": {k: {"aliases": v.get("aliases", [])} for k, v in cfg.get("apps", {}).items()},
                "websites": cfg.get("websites", {}),
                "actions": [{k: a.get(k) for k in ("name", "say", "phrases", "confirm", "background", "done", "ends_session")}
                            for a in cfg.get("actions", []) if a.get("enabled", True)],
            })
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        if self.path != "/run":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        result = run(str(body.get("name", "")), str(body.get("arg", "")))
        print(f"run {body.get('name')} {body.get('arg', '')!r} -> {result.get('ok')}", flush=True)
        return self._send(200, result)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"Jarvis Pi helper on port {PORT}, actions from {ACTIONS_FILE}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
