#!/usr/bin/env python3
"""
runner.py — tiny HTTP bridge so n8n (in Docker) can run agent.py on the host.

n8n runs inside a container: it can't see ~/projects/miscellaneous-ken, and it
has no python, ffmpeg, yt-dlp or your YouTube OAuth token. So instead of an
Execute Command node, n8n makes an HTTP call to this service, which runs on
the Mac itself with your real environment.

    n8n (docker)  --POST http://host.docker.internal:8765/run-->  runner.py  -->  agent.py

Endpoints
    GET  /health   -> {"ok": true, "busy": false}          (no auth)
    POST /run      -> runs agent.py, returns the result    (auth required)
         body (all optional): {"dry_run": false, "limit": 0, "no_llm": false}

Auth
    Every /run request must carry  X-Runner-Token: <token>  matching the
    MKEN_RUNNER_TOKEN environment variable. The service refuses to start
    without one.

Binding
    Defaults to 0.0.0.0 because Docker Desktop traffic arriving via
    host.docker.internal does NOT come from 127.0.0.1 — binding to loopback
    would refuse the container. That means anything on your local network can
    reach the port, which is exactly why the token is mandatory. On an
    untrusted network, either firewall the port or run n8n with
    `--add-host` and bind to that interface instead.

Run it
    export MKEN_RUNNER_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
    export ANTHROPIC_API_KEY="sk-ant-..."
    .venv/bin/python3 runner.py

Keep it running across reboots with launchd — see README.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent

HOST = os.environ.get("MKEN_RUNNER_HOST", "0.0.0.0")
PORT = int(os.environ.get("MKEN_RUNNER_PORT", "8765"))
TOKEN = os.environ.get("MKEN_RUNNER_TOKEN", "")
# agent.py downloads and re-encodes video, so give it real headroom.
TIMEOUT = int(os.environ.get("MKEN_RUNNER_TIMEOUT", "900"))

# Prefer the project venv's interpreter, fall back to whatever started us.
VENV_PY = ROOT / ".venv" / "bin" / "python3"
PYTHON = str(VENV_PY) if VENV_PY.exists() else sys.executable

# Only one run at a time: two overlapping runs would race on state.json and
# could post the same story twice.
_lock = threading.Lock()
_last: dict = {}


def _tail(text: str, lines: int = 40, chars: int = 4000) -> str:
    out = "\n".join(text.splitlines()[-lines:])
    return out[-chars:]


def run_agent(dry_run: bool = False, limit: int = 0, no_llm: bool = False) -> dict:
    cmd = [PYTHON, str(ROOT / "agent.py"), "-v"]
    if dry_run:
        cmd.append("--dry-run")
    if no_llm:
        cmd.append("--no-llm")
    if limit:
        cmd += ["--limit", str(int(limit))]

    started = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=TIMEOUT
        )
        code, out, err = proc.returncode, proc.stdout, proc.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        code, timed_out = -1, True
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        err += f"\n[runner] killed after {TIMEOUT}s"

    # agent.py prints one "MKEN_SUMMARY {...}" line to stdout; everything else
    # (the log narrative) goes to stderr.
    summary = None
    for line in reversed(out.splitlines()):
        if line.startswith("MKEN_SUMMARY "):
            try:
                summary = json.loads(line[len("MKEN_SUMMARY "):])
            except json.JSONDecodeError:
                pass
            break

    result = {
        "ok": code == 0 and not timed_out,
        "exit_code": code,
        "timed_out": timed_out,
        "duration_seconds": round(time.time() - started, 1),
        "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "command": " ".join(cmd),
        "posted": (summary or {}).get("posted", 0),
        "picks": (summary or {}).get("picks", 0),
        "candidates": (summary or {}).get("candidates", 0),
        "headlines": (summary or {}).get("headlines", []),
        "summary": summary,
        "log": _tail(err),
    }
    globals()["_last"] = result
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "MkenRunner/1.0"

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        supplied = self.headers.get("X-Runner-Token", "")
        # Constant-time compare so the token can't be guessed by timing.
        return bool(supplied) and secrets.compare_digest(supplied, TOKEN)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/health", ""):
            self._send(200, {
                "ok": True,
                "busy": _lock.locked(),
                "python": PYTHON,
                "project": str(ROOT),
                "last_run": _last or None,
            })
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/run":
            self._send(404, {"ok": False, "error": "not found"})
            return
        if not self._authorised():
            self._send(401, {"ok": False, "error": "bad or missing X-Runner-Token"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "error": "body must be JSON"})
            return

        if not _lock.acquire(blocking=False):
            # 409 rather than queueing: a run is already in flight, and the
            # next schedule tick will pick things up anyway.
            self._send(409, {"ok": False, "error": "a run is already in progress"})
            return
        try:
            result = run_agent(
                dry_run=bool(body.get("dry_run", False)),
                limit=int(body.get("limit", 0) or 0),
                no_llm=bool(body.get("no_llm", False)),
            )
        finally:
            _lock.release()

        # Always HTTP 200 — the JSON says whether the run succeeded, so n8n
        # can branch on it instead of treating a failed run as a dead endpoint.
        self._send(200, result)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"{self.log_date_time_string()}  {fmt % args}\n")


def main() -> int:
    if not TOKEN:
        sys.exit(
            "MKEN_RUNNER_TOKEN is not set — refusing to start.\n\n"
            "Generate one:\n"
            "  export MKEN_RUNNER_TOKEN=\"$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')\"\n"
        )
    if not (ROOT / "agent.py").exists():
        sys.exit(f"agent.py not found next to runner.py in {ROOT}")

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"mken runner listening on http://{HOST}:{PORT}")
    print(f"  python:  {PYTHON}")
    print(f"  project: {ROOT}")
    print(f"  timeout: {TIMEOUT}s")
    print("  n8n (docker) should call http://host.docker.internal:"
          f"{PORT}/run with the X-Runner-Token header")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
