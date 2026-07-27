#!/usr/bin/env python3
"""Run this to bring Flow Agent online at https://flow.kodelyx.in
Starts the backend (flow serve) + the Cloudflare Tunnel, keeps both alive."""
import os
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_FLOW = os.path.join(ROOT, ".venv", "bin", "flow")
CF_CONFIG = os.path.join(ROOT, "cloudflared", "config.yml")

procs = {}


def backend_is_up():
    try:
        urllib.request.urlopen("http://127.0.0.1:8001/health", timeout=2)
        return True
    except Exception:
        return False


def start_backend():
    print("[connect] starting flow-agent backend (flow serve)...")
    return subprocess.Popen([VENV_FLOW, "serve"], cwd=ROOT)


def start_tunnel():
    print("[connect] starting cloudflared tunnel -> flow.kodelyx.in ...")
    return subprocess.Popen(["cloudflared", "tunnel", "--config", CF_CONFIG, "run"], cwd=ROOT)


def stop_all(*_):
    print("\n[connect] stopping...")
    for name, p in procs.items():
        if p and p.poll() is None:
            p.terminate()
    sys.exit(0)


def main():
    signal.signal(signal.SIGINT, stop_all)
    signal.signal(signal.SIGTERM, stop_all)

    if backend_is_up():
        print("[connect] backend already running, reusing it.")
        procs["backend"] = None
    else:
        procs["backend"] = start_backend()
        for _ in range(30):
            if backend_is_up():
                break
            time.sleep(1)

    procs["tunnel"] = start_tunnel()

    print("[connect] up. https://flow.kodelyx.in is live. Ctrl+C to stop.")
    while True:
        time.sleep(5)
        if procs["backend"] is not None and procs["backend"].poll() is not None:
            print("[connect] backend died, restarting...")
            procs["backend"] = start_backend()
        if procs["tunnel"].poll() is not None:
            print("[connect] tunnel died, restarting...")
            procs["tunnel"] = start_tunnel()


if __name__ == "__main__":
    main()
