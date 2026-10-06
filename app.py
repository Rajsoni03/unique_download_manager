#!/usr/bin/env python3
"""Unique Download Manager — self-hosted parallel multi-network download server."""

import argparse
import json
import os
import sys
import time

from flask import Flask, Response, jsonify, request, send_file

from engine.manager import DownloadManager, default_download_dir

app = Flask(__name__, static_folder="static", static_url_path="/static")

MANAGER: DownloadManager = None  # type: ignore[assignment]


# --------------------------------------------------------------------- helpers

def _json_error(message: str, status: int = 400) -> tuple:
    return jsonify({"error": message}), status


def _task_json(task) -> tuple:
    return jsonify(task.to_dict()), 200


# ------------------------------------------------------------------------ core

@app.after_request
def add_cors(resp):
    # LAN devices need open access to the API.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return resp


@app.route("/api/<path:_>", methods=["OPTIONS"])
@app.route("/api", methods=["OPTIONS"])
def cors_preflight(_=""):
    return ("", 204)


@app.get("/")
def index():
    return send_file(os.path.join(app.static_folder, "index.html"))


@app.get("/api/snapshot")
def snapshot():
    return jsonify(MANAGER.snapshot())


@app.get("/api/events")
def events():
    """Server-Sent Events: full state snapshot every ~0.6 s."""
    def stream():
        last_id = ""
        while True:
            snap = MANAGER.snapshot()
            payload = json.dumps(snap, separators=(",", ":"))
            event_id = str(snap.get("server_time", ""))
            if payload != last_id:
                last_id = payload
                yield f"data: {payload}\n\n"
            else:
                yield ": ping\n\n"
            time.sleep(0.6)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------------- tasks

@app.post("/api/add")
def add_task():
    data = request.get_json(silent=True) or {}
    try:
        task = MANAGER.add(
            url=data.get("url", ""),
            directory=data.get("directory") or None,
            priority=int(data.get("priority", 0)),
        )
    except ValueError as exc:
        return _json_error(str(exc))
    return _task_json(task)


@app.get("/api/tasks")
def list_tasks():
    return jsonify([t.to_dict() for t in MANAGER.snapshot()["tasks"]])


def _task_action(fn):
    task_id = request.view_args["task_id"]
    try:
        fn(task_id)
    except KeyError as exc:
        return _json_error(str(exc), 404)
    except ValueError as exc:
        return _json_error(str(exc))
    return jsonify({"ok": True})


@app.post("/api/tasks/<task_id>/pause")
def pause_task(task_id):
    return _task_action(MANAGER.pause)


@app.post("/api/tasks/<task_id>/resume")
def resume_task(task_id):
    return _task_action(MANAGER.resume)


@app.post("/api/tasks/<task_id>/cancel")
def cancel_task(task_id):
    return _task_action(MANAGER.cancel)


@app.post("/api/tasks/<task_id>/priority")
def priority_task(task_id):
    data = request.get_json(silent=True) or {}
    delta = int(data.get("delta", 0))
    return _task_action(lambda tid: MANAGER.set_priority(tid, delta))


@app.delete("/api/tasks/<task_id>")
def delete_task(task_id):
    return _task_action(MANAGER.remove)


# ------------------------------------------------------------------- settings

@app.get("/api/settings")
def get_settings():
    s = MANAGER.settings
    return jsonify({
        "directory": s.directory,
        "max_downloads": s.max_downloads,
        "connections": s.connections,
        "enabled_ifaces": s.enabled_ifaces,
    })


@app.post("/api/settings")
def set_settings():
    data = request.get_json(silent=True) or {}
    try:
        MANAGER.update_settings(data)
    except (ValueError, TypeError) as exc:
        return _json_error(str(exc))
    return get_settings()


# --------------------------------------------------------------- file browser

@app.get("/api/fs")
def browse_fs():
    path = request.args.get("path") or ""
    path = os.path.expanduser(path) if path else os.path.expanduser("~")
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return _json_error(f"Not a directory: {path}")

    show_hidden = request.args.get("hidden") == "1"
    dirs = []
    try:
        for entry in sorted(os.scandir(path), key=lambda e: e.name.lower()):
            if not entry.is_dir(follow_symlinks=True):
                continue
            if not show_hidden and entry.name.startswith("."):
                continue
            dirs.append(entry.name)
    except PermissionError:
        return _json_error(f"Permission denied: {path}", 403)

    quick = []
    home = os.path.expanduser("~")
    for label, p in [("Home", home), ("Downloads", os.path.join(home, "Downloads")),
                     ("Root", "/")]:
        if os.path.isdir(p):
            quick.append({"label": label, "path": p})

    return jsonify({
        "path": path,
        "parent": os.path.dirname(path) if path != os.path.dirname(path) else None,
        "dirs": dirs,
        "quick": quick,
        "writable": os.access(path, os.W_OK),
    })


@app.post("/api/open")
def open_path():
    data = request.get_json(silent=True) or {}
    path = data.get("path", "")
    if not path or not os.path.exists(path):
        return _json_error("Path does not exist", 404)
    try:
        MANAGER.reveal(path)
    except Exception as exc:  # noqa: BLE001
        return _json_error(f"Could not open: {exc}", 500)
    return jsonify({"ok": True})


# ------------------------------------------------------------------- entrypoint

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Unique Download Manager")
    p.add_argument("--host", default=os.environ.get("UDM_HOST", "127.0.0.1"),
                   help="Bind address (use 0.0.0.0 to allow phone/LAN access)")
    p.add_argument("--port", type=int, default=int(os.environ.get("UDM_PORT", 8765)))
    p.add_argument("--dir", default=os.environ.get("UDM_DIR"),
                   help="Default download directory")
    p.add_argument("--data-dir", default=os.environ.get("UDM_DATA_DIR"),
                   help="Where settings/state are stored")
    p.add_argument("--debug", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    global MANAGER
    args = parse_args(argv)
    data_dir = args.data_dir or os.path.join(
        os.environ.get("UDM_HOME")
        or os.path.join(os.path.dirname(os.path.abspath(__file__)), "db"))
    default_dir = args.dir or default_download_dir()
    MANAGER = DownloadManager(data_dir=data_dir, default_dir=default_dir)

    print(f"\n  ⬇  Unique Download Manager")
    print(f"     UI:      http://{args.host}:{args.port}")
    print(f"     Files:   {MANAGER.settings.directory}")
    print(f"     Data:    {data_dir}")
    nets = ", ".join(f"{n['kind']}({n['ip']})" for n in MANAGER.monitor.snapshot()) or "none"
    print(f"     Networks: {nets}\n")

    try:
        app.run(host=args.host, port=args.port, debug=args.debug,
                threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        MANAGER.shutdown()


if __name__ == "__main__":
    sys.exit(main())
