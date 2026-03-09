"""Flask backend for Booster Web UI: subprocess management + SSE streaming."""
import json
import os
import queue
import re
import subprocess
import sys
import threading

from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)

# Subprocess state
process = None
log_queue = queue.Queue()
status_data = {
    "running": False, "phase": "idle", "bv": "", "target": 0,
    "hits": 0, "initial_views": 0, "current_views": 0, "views_increase": 0,
    "total_proxies": 0, "active_proxies": 0,
}


def get_script_dir():
    return os.path.dirname(os.path.abspath(__file__))


_RE_STATS = re.compile(r'\[Hits:\s*(\d+),\s*Views\+:\s*(-?\d+)\]')
_RE_VIEWS = re.compile(r'^(\d+)/(\d+)\s')
_RE_FILTER = re.compile(r'successfully filter (\d+)')
_RE_COLLECTED = re.compile(r'collected (\d+) proxies')


def reader():
    """Read subprocess stdout line by line, push to queue."""
    global process
    p = process
    if p is None or p.stdout is None:
        return
    try:
        for line in iter(p.stdout.readline, ""):
            line = line.rstrip("\n\r")
            if not line:
                continue

            # Parse stats before queuing
            m = _RE_STATS.search(line)
            if m:
                status_data["hits"] = int(m.group(1))
                status_data["views_increase"] = int(m.group(2))
            vm = _RE_VIEWS.match(line)
            if vm:
                status_data["current_views"] = int(vm.group(1))
            if "Initial view count:" in line:
                try:
                    v = int(line.split(":")[-1].strip())
                    status_data["initial_views"] = v
                    status_data["current_views"] = v
                except (IndexError, ValueError):
                    pass
            fm = _RE_FILTER.search(line)
            if fm:
                status_data["active_proxies"] = int(fm.group(1))
                status_data["phase"] = "boosting"
            cm = _RE_COLLECTED.search(line)
            if cm:
                status_data["total_proxies"] = int(cm.group(1))
                status_data["phase"] = "filtering"

            log_queue.put({"type": "log", "line": line})
    finally:
        if p:
            p.wait()
        process = None
        log_queue.put(None)
        status_data["running"] = False
        status_data["phase"] = "idle"


def start_booster(bv: str, target: int) -> tuple[bool, str]:
    """Start booster subprocess. Returns (ok, message)."""
    global process
    if process is not None:
        return False, "Already running"
    # Drain leftover items from previous run
    while True:
        try:
            log_queue.get_nowait()
        except queue.Empty:
            break
    try:
        booster_path = os.path.join(get_script_dir(), "booster.py")
        process = subprocess.Popen(
            [sys.executable, booster_path, bv, str(target)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=get_script_dir(),
        )
        status_data["running"] = True
        status_data["phase"] = "fetching"
        status_data["bv"] = bv
        status_data["target"] = target
        status_data["hits"] = 0
        status_data["initial_views"] = 0
        status_data["current_views"] = 0
        status_data["views_increase"] = 0
        status_data["total_proxies"] = 0
        status_data["active_proxies"] = 0
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        return True, "Started"
    except Exception as e:
        return False, str(e)


def stop_booster() -> bool:
    """Terminate subprocess. Returns True if stopped."""
    global process
    if process is None:
        return False
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    process = None
    return True


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json() or {}
    bv = (data.get("bv") or "").strip()
    target = data.get("target")
    if not bv:
        return jsonify({"ok": False, "message": "BV/AV id is required"}), 400
    try:
        target = int(target)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Target view count must be a number"}), 400
    if target < 1:
        return jsonify({"ok": False, "message": "Target must be >= 1"}), 400
    ok, msg = start_booster(bv, target)
    if ok:
        return jsonify({"ok": True, "message": msg})
    return jsonify({"ok": False, "message": msg}), 409


@app.route("/stop", methods=["POST"])
def stop():
    if stop_booster():
        return jsonify({"ok": True, "message": "Stopped"})
    return jsonify({"ok": False, "message": "Not running"}), 409


@app.route("/status")
def status():
    return jsonify(status_data)


@app.route("/stream")
def stream():
    def gen():
        while True:
            try:
                item = log_queue.get(timeout=2)
                if item is None:
                    yield f"data: {json.dumps({'type': 'done', **status_data})}\n\n"
                    break
                item["status"] = dict(status_data)
                yield f"data: {json.dumps(item)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'heartbeat', **status_data})}\n\n"

    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
