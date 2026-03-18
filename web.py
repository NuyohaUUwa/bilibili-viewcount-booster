"""Flask backend for Booster Web UI: multi-task subprocess management + SSE streaming."""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time as _time
import uuid
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(SCRIPT_DIR, './logs/run_history.log')

# --- Multi-task state ---
tasks = {}  # task_id -> TaskState
tasks_lock = threading.Lock()

_RE_STATS = re.compile(r'\[Hits:\s*(\d+),\s*Views\+:\s*(-?\d+)\]')
_RE_VIEWS = re.compile(r'^(\d+)/(\d+)\s')
_RE_FILTER = re.compile(r'successfully filter (\d+)')
_RE_COLLECTED = re.compile(r'collected (\d+) proxies')
_RE_REMOVED = re.compile(r'removed (\d+) dead proxies, (\d+) remaining')
_RE_POOL_NOW = re.compile(r'pool now (\d+)')
_RE_BV_IN_URL = re.compile(r'(BV[0-9A-Za-z]{10})', re.IGNORECASE)


class TaskState:
    def __init__(self, task_id: str, bv: str, target: int):
        self.id = task_id
        self.bv = bv
        self.target = target
        self.process = None
        self.log_queue = queue.Queue()
        self.started_at = _time.time()
        self.history_written = False
        self.status = {
            "id": task_id, "running": True, "phase": "fetching",
            "bv": bv, "target": target,
            "hits": 0, "initial_views": 0, "current_views": 0, "views_increase": 0,
            "total_proxies": 0, "active_proxies": 0, "started_at": self.started_at,
        }

    def to_summary(self) -> dict:
        d = dict(self.status)
        d["elapsed"] = int(_time.time() - self.started_at) if self.status["running"] else d.get("elapsed", 0)
        return d


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}min {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}min"


def _write_history(task: TaskState):
    """Append one task result line to run_history.log (called once per task)."""
    if task.history_written:
        return
    task.history_written = True
    try:
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    except Exception:
        pass
    s = task.status
    elapsed = s.get("elapsed", int(_time.time() - task.started_at))
    record = {
        "bv": task.bv,
        "target": task.target,
        "phase": s.get("phase", ""),
        "elapsed": elapsed,
        "elapsed_fmt": _fmt_duration(elapsed),
        "hits": s.get("hits", 0),
        "initial_views": s.get("initial_views", 0),
        "current_views": s.get("current_views", 0),
        "views_increase": s.get("views_increase", 0),
        "active_proxies": s.get("active_proxies", 0),
        "total_proxies": s.get("total_proxies", 0),
        "started_at": datetime.fromtimestamp(task.started_at).strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(HISTORY_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception:
        pass


def _extract_bv_or_raw(text: str) -> str:
    """Allow user to paste full video URL; try to extract BV号."""
    t = (text or "").strip()
    if not t:
        return t
    lower = t.lower()
    # 已经是 BV / AV / 纯数字，就直接用
    if lower.startswith("bv") or lower.startswith("av") or t.isdigit():
        return t
    m = _RE_BV_IN_URL.search(t)
    if m:
        return m.group(1)
    return t


def get_script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _reader(task: TaskState):
    p = task.process
    if p is None or p.stdout is None:
        return
    try:
        for line in iter(p.stdout.readline, ""):
            line = line.rstrip("\n\r")
            if not line:
                continue
            s = task.status
            m = _RE_STATS.search(line)
            if m:
                s["hits"] = int(m.group(1))
                s["views_increase"] = int(m.group(2))
            vm = _RE_VIEWS.match(line)
            if vm:
                s["current_views"] = int(vm.group(1))
            if "Initial view count:" in line:
                try:
                    v = int(line.split(":")[-1].strip())
                    s["initial_views"] = v
                    s["current_views"] = v
                except (IndexError, ValueError):
                    pass
            fm = _RE_FILTER.search(line)
            if fm:
                s["active_proxies"] = int(fm.group(1))
                s["phase"] = "boosting"
            cm = _RE_COLLECTED.search(line)
            if cm:
                s["total_proxies"] = int(cm.group(1))
                s["phase"] = "filtering"
            rm = _RE_REMOVED.search(line)
            if rm:
                s["active_proxies"] = int(rm.group(2))
            pm = _RE_POOL_NOW.search(line)
            if pm:
                s["active_proxies"] = int(pm.group(1))
            if "refreshing proxy pool" in line:
                s["phase"] = "refreshing"
            elif s.get("phase") == "refreshing" and ("added" in line or "no new" in line or "refresh failed" in line):
                s["phase"] = "boosting"
            task.log_queue.put({"type": "log", "line": line, "task_id": task.id})
    finally:
        if p:
            p.wait()
        task.process = None
        task.status["running"] = False
        task.status["phase"] = "finished"
        task.status["elapsed"] = int(_time.time() - task.started_at)
        _write_history(task)
        task.log_queue.put(None)


def start_task(bv: str, target: int) -> tuple[bool, str, str]:
    """Start a new booster task. Returns (ok, message, task_id)."""
    task_id = uuid.uuid4().hex[:8]
    task = TaskState(task_id, bv, target)
    try:
        booster_path = os.path.join(get_script_dir(), "booster.py")
        task.process = subprocess.Popen(
            [sys.executable, booster_path, bv, str(target)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=get_script_dir(),
        )
        with tasks_lock:
            tasks[task_id] = task
        t = threading.Thread(target=_reader, args=(task,), daemon=True)
        t.start()
        return True, "Started", task_id
    except Exception as e:
        return False, str(e), ""


def stop_task(task_id: str) -> bool:
    with tasks_lock:
        task = tasks.get(task_id)
    if task is None or task.process is None:
        return False
    task.process.terminate()
    try:
        task.process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        task.process.kill()
    task.process = None
    task.status["running"] = False
    task.status["phase"] = "stopped"
    task.status["elapsed"] = int(_time.time() - task.started_at)
    _write_history(task)
    return True


def remove_task(task_id: str) -> bool:
    with tasks_lock:
        task = tasks.get(task_id)
        if task is None:
            return False
        if task.process is not None:
            task.process.terminate()
            try:
                task.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                task.process.kill()
        del tasks[task_id]
    return True


# --- Routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json() or {}
    bv_raw = (data.get("bv") or "").strip()
    bv = _extract_bv_or_raw(bv_raw)
    target = data.get("target")
    if not bv:
        return jsonify({"ok": False, "message": "BV/AV id is required"}), 400
    try:
        target = int(target)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Target view count must be a number"}), 400
    if target < 1:
        return jsonify({"ok": False, "message": "Target must be >= 1"}), 400
    ok, msg, task_id = start_task(bv, target)
    if ok:
        return jsonify({"ok": True, "message": msg, "task_id": task_id})
    return jsonify({"ok": False, "message": msg}), 500


@app.route("/stop/<task_id>", methods=["POST"])
def stop(task_id):
    if stop_task(task_id):
        return jsonify({"ok": True, "message": "Stopped"})
    return jsonify({"ok": False, "message": "Not found or not running"}), 404


@app.route("/remove/<task_id>", methods=["POST"])
def remove(task_id):
    if remove_task(task_id):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "message": "Not found"}), 404


@app.route("/tasks")
def list_tasks():
    with tasks_lock:
        summaries = [t.to_summary() for t in tasks.values()]
    return jsonify(summaries)


@app.route("/history")
def history():
    """Return latest run record per BV (only newest for each video)."""
    latest_by_bv: dict[str, dict] = {}
    try:
        with open(HISTORY_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                bv = rec.get("bv")
                if not bv:
                    continue
                # 同一 BV 始终保留最新（文件是追加写入的，后面覆盖前面）
                latest_by_bv[bv] = rec
    except FileNotFoundError:
        pass
    records = list(latest_by_bv.values())
    # 按结束时间倒序
    records.sort(key=lambda r: r.get("finished_at", "") or "", reverse=True)
    return jsonify(records)


@app.route("/stream/<task_id>")
def stream(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if task is None:
        return jsonify({"error": "task not found"}), 404

    def gen():
        while True:
            try:
                item = task.log_queue.get(timeout=2)
                if item is None:
                    yield f"data: {json.dumps({'type': 'done', **task.to_summary()})}\n\n"
                    break
                item["status"] = task.to_summary()
                yield f"data: {json.dumps(item)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'heartbeat', **task.to_summary()})}\n\n"

    return Response(
        gen(), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
