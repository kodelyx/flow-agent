#!/usr/bin/env python3
"""Sniff server — captures all Flow UI API requests.

Start this, then use Flow UI normally. Every API call gets logged.
Useful for discovering new endpoints, model keys, and payload formats.

Usage:
    python sniff.py
    python sniff.py --save sniffed.json
"""

import asyncio
import argparse
import json
import logging
import signal
import sys
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

for pkg in ["websockets"]:
    try:
        __import__(pkg)
    except ImportError:
        os.system(f"{sys.executable} -m pip install {pkg} -q")

import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("sniff")

# ─── Storage ─────────────────────────────────────────────────

all_requests = []

# Noise URLs to ignore
IGNORE = {"batchLog", "frontendEvents", "fetchUserRecommendations", "flowAgent/applets",
          "savedSharedApplets", "models/statuses"}


# ─── HTTP Handler ────────────────────────────────────────────

def make_handler(save_file):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            if body.get("type") == "sniffed_video_request":
                url = body.get("url", "")

                # Skip noise
                if not any(n in url for n in IGNORE):
                    method = body.get("method", "?")
                    payload = body.get("payload", "")

                    # Pretty print
                    log.info("─" * 60)
                    log.info("🔍 %s %s", method, url.split("?")[0])
                    if payload and payload != "(empty)":
                        try:
                            parsed = json.loads(payload)
                            log.info("   %s", json.dumps(parsed, indent=2)[:2000])
                        except (json.JSONDecodeError, TypeError):
                            log.info("   %s", str(payload)[:1000])

                    entry = {
                        "url": url,
                        "method": method,
                        "payload": payload,
                        "timestamp": body.get("timestamp"),
                    }
                    all_requests.append(entry)

                    # Auto-save
                    if save_file:
                        with open(save_file, "w") as f:
                            json.dump(all_requests, f, indent=2)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def log_message(self, *a):
            pass

    return Handler


# ─── Main ────────────────────────────────────────────────────

async def on_connect(ws):
    log.info("✅ Extension connected!")
    async for raw in ws:
        data = json.loads(raw)
        if data.get("type") == "token_captured":
            log.info("🔑 Token captured")


async def main():
    parser = argparse.ArgumentParser(description="Flow API Sniffer")
    parser.add_argument("--save", "-s", help="Save sniffed requests to JSON file")
    parser.add_argument("--port", type=int, default=8100, help="HTTP port (default: 8100)")
    parser.add_argument("--ws-port", type=int, default=9222, help="WS port (default: 9222)")
    args = parser.parse_args()

    Handler = make_handler(args.save)
    srv = HTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    log.info("⚡ WS server on ws://127.0.0.1:%d", args.ws_port)
    log.info("⚡ HTTP callback on http://127.0.0.1:%d", args.port)
    if args.save:
        log.info("💾 Saving to: %s", args.save)
    log.info("👉 Open Flow UI and perform any action...")
    log.info("─" * 60)

    async with websockets.serve(on_connect, "127.0.0.1", args.ws_port):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
