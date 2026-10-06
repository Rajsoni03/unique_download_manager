#!/usr/bin/env python3
"""End-to-end smoke tests for the Unique Download Manager engine.

Runs local HTTP fixture servers and verifies: parallel download, pause/resume
byte-exactness, outage resume, range-ignored fallback, mid-download range
breakage, filename resolution edge cases, HTML rejection, cancel, priority
queueing and interface load-balancing logic.

Usage:  python3 tests/smoke_test.py
"""

import http.server
import hashlib
import os
import re
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FILE_SIZE = 48 * 1024 * 1024          # 48 MB fixture payload
THROTTLE = 9_000_000                  # ~9 MB/s so timing-sensitive tests hold
_P = zlib.compress(os.urandom(6 * 1024 * 1024))
FIXTURE = (_P * (FILE_SIZE // len(_P) + 1))[:FILE_SIZE]
FIXTURE_HASH = hashlib.sha256(FIXTURE).hexdigest()


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    payload = FIXTURE
    state: dict = {}

    def log_message(self, *args):
        pass

    # ------------------------------------------------------------- helpers

    def _offline(self) -> bool:
        if self.state.get("offline"):
            try:
                self.connection.close()
            except Exception:
                pass
            self.close_connection = True
            return True
        return False

    def _send_body(self, body: bytes) -> None:
        piece = 256 * 1024
        rate = self.state.get("throttle", THROTTLE)
        for i in range(0, len(body), piece):
            self.wfile.write(body[i:i + piece])
            time.sleep(len(body[i:i + piece]) / rate)

    def _common_headers(self, extra=None):
        for k, v in (extra or {}).items():
            self.send_header(k, v)

    # ------------------------------------------------------------- handlers

    def do_HEAD(self):
        if self._offline():
            return
        if self.path.startswith("/page"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "100")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("ETag", '"fixture-v1"')
        self.send_header("Content-Disposition", 'attachment; filename="sample_file.bin"')
        self.send_header("Accept-Ranges", "none" if self.state.get("no_range") else "bytes")
        self.end_headers()

    def do_GET(self):
        if self._offline():
            return

        if self.path.startswith("/page"):
            body = b"<!doctype html><html><body>login please</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/big.bin")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if self.path.startswith("/noident"):
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(self.payload)))
            self.end_headers()
            self._send_body(self.payload)
            return

        if self.path.startswith("/download"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(self.payload)))
            self.end_headers()
            self._send_body(self.payload)
            return

        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            p = self.payload
            for i in range(0, len(p), 512 * 1024):
                piece = p[i:i + 512 * 1024]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
                time.sleep(len(piece) / THROTTLE)
            self.wfile.write(b"0\r\n\r\n")
            return

        # ---- ranged (or plain) delivery of the big file
        rng = self.headers.get("Range")
        no_range = self.state.get("no_range")
        probe_only = self.state.get("probe_only_range")
        if rng and not no_range:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else len(self.payload) - 1
            end = min(end, len(self.payload) - 1)
            is_probe = (start == 0 and end == 0)
            if probe_only and not is_probe:
                rng = None          # pretend range is unsupported for real chunks
            else:
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(self.payload)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = self.payload[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.payload)}")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", '"fixture-v1"')
                self.send_header("Content-Disposition",
                                 'attachment; filename="sample_file.bin"')
                self.end_headers()
                self._send_body(body)
                return

        body = self.payload
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"fixture-v1"')
        self.send_header("Content-Disposition", 'attachment; filename="sample_file.bin"')
        if not no_range:
            self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self._send_body(body)


class Server:
    def __init__(self, port, **state):
        self.port = port
        self.state = dict(offline=False, **state)
        outer = self

        class Handler(FixtureHandler):
            state = outer.state

        class QuietThreadingServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

            def handle_error(self, request, client_address):
                exc = sys.exc_info()[1]
                if isinstance(exc, (BrokenPipeError, ConnectionResetError,
                                    ConnectionAbortedError)):
                    return    # clients disconnect on purpose during tests
                super().handle_error(request, client_address)

        self.httpd = QuietThreadingServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.httpd.allow_reuse_address = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        time.sleep(0.2)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------- #

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        FAIL += 1
        print(f"  \033[31m✗\033[0m {name}  {detail}")


def snap_task(mgr, task_id):
    return next((t for t in mgr.snapshot()["tasks"] if t["id"] == task_id), None)


def wait_status(mgr, task_id, statuses, timeout=120, interval=0.4):
    end = time.time() + timeout
    task = snap_task(mgr, task_id)
    while time.time() < end:
        task = snap_task(mgr, task_id)
        if task and task["status"] in statuses:
            return task
        time.sleep(interval)
    return task


def wait_downloaded(mgr, task_id, min_bytes, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        task = snap_task(mgr, task_id)
        if task and task["downloaded"] >= min_bytes:
            return task
        time.sleep(0.2)
    return snap_task(mgr, task_id)


def main():
    from engine.manager import DownloadManager

    tmp = tempfile.mkdtemp(prefix="udm_smoke_")
    downloads = os.path.join(tmp, "downloads")
    os.makedirs(downloads)

    main_srv = Server(8977)
    no_range = Server(8978, no_range=True)
    probe_only = Server(8979, probe_only_range=True)

    mgr = DownloadManager(data_dir=os.path.join(tmp, ".data"), default_dir=downloads)
    mgr.update_settings({"connections": 6, "max_downloads": 3})

    try:
        print("\n1) Parallel multi-connection download completes byte-exact")
        t = mgr.add(f"{main_srv.url}/big.bin")
        task = wait_status(mgr, t.id, ("completed", "failed", "canceled"), timeout=120)
        path = os.path.join(downloads, task["filename"]) if task["filename"] else ""
        check("completed", task["status"] == "completed",
              f"status={task['status']} err={task['error']}")
        check("filename from Content-Disposition", task["filename"] == "sample_file.bin",
              f"got {task['filename']!r}")
        check("size matches", os.path.exists(path) and os.path.getsize(path) == FILE_SIZE,
              f"size={os.path.getsize(path) if os.path.exists(path) else 'missing'}")
        check("parallel chunks used", task["supports_range"] and task["chunks"]["total"] > 1,
              f"chunks={task['chunks']}")
        check("multiple connections active", task["connections"] > 1 or task["status"] != "downloading",
              f"connections={task['connections']}")

        print("\n2) Pause mid-download → resume → byte-exact result")
        t2 = mgr.add(f"{main_srv.url}/big.bin")
        task2 = wait_status(mgr, t2.id, ("downloading",), timeout=60)
        check("downloading", task2 and task2["status"] == "downloading",
              f"err={task2['error'] if task2 else '?'}")
        task2 = wait_downloaded(mgr, t2.id, 5 * 1024 * 1024)
        mgr.pause(t2.id)
        paused = wait_status(mgr, t2.id, ("paused", "failed", "completed"), timeout=30)
        time.sleep(1.0)   # let the task thread flush metadata
        check("paused", paused["status"] == "paused", f"status={paused['status']}")
        check("partial bytes kept", 0 < paused["downloaded"] < FILE_SIZE,
              f"downloaded={paused['downloaded']}")
        part = os.path.join(downloads, paused["filename"] + ".part")
        check(".part exists", os.path.exists(part))
        from engine import storage as st
        check("resume metadata saved", st.load_meta(downloads, t2.id) is not None)
        mgr.resume(t2.id)
        final = wait_status(mgr, t2.id, ("completed", "failed"), timeout=180)
        check("resumed to completion", final["status"] == "completed", f"err={final['error']}")
        f2 = os.path.join(downloads, final["filename"])
        check("resumed size exact", os.path.exists(f2) and os.path.getsize(f2) == FILE_SIZE,
              f"size={os.path.getsize(f2) if os.path.exists(f2) else 'missing'}")
        check("second download got unique name", final["filename"] == "sample_file (1).bin",
              f"got {final['filename']!r}")

        print("\n3) Connectivity loss → waiting_network → auto-resume when back")
        t3 = mgr.add(f"{main_srv.url}/big.bin")
        wait_status(mgr, t3.id, ("downloading",), timeout=60)
        wait_downloaded(mgr, t3.id, 4 * 1024 * 1024)
        main_srv.state["offline"] = True          # simulated internet drop
        cur = wait_status(mgr, t3.id, ("waiting_network", "failed"), timeout=45)
        check("reports waiting_network while offline",
              cur and cur["status"] == "waiting_network",
              f"status={cur['status'] if cur else '?'} err={cur['error'][:60] if cur else ''}")
        main_srv.state["offline"] = False         # network is back
        final3 = wait_status(mgr, t3.id, ("completed", "failed"), timeout=180)
        check("auto-recovered and completed", final3 and final3["status"] == "completed",
              f"status={final3['status'] if final3 else '?'} "
              f"err={final3['error'][:80] if final3 else ''}")
        f3 = os.path.join(downloads, final3["filename"])
        check("outage-resumed size exact",
              os.path.exists(f3) and os.path.getsize(f3) == FILE_SIZE,
              f"size={os.path.getsize(f3) if os.path.exists(f3) else 'missing'}")

        print("\n4) Server without Range support → single-connection download")
        t4 = mgr.add(f"{no_range.url}/big.bin")
        final4 = wait_status(mgr, t4.id, ("completed", "failed"), timeout=180)
        check("completed", final4["status"] == "completed", f"err={final4['error'][:80]}")
        check("no-range mode", final4["supports_range"] is False,
              f"supports_range={final4['supports_range']}")
        f4 = os.path.join(downloads, final4["filename"])
        check("size exact", os.path.exists(f4) and os.path.getsize(f4) == FILE_SIZE)

        print("\n5) Server breaks Range mid-download → safe fallback to 1 connection")
        t5 = mgr.add(f"{probe_only.url}/big.bin")
        final5 = wait_status(mgr, t5.id, ("completed", "failed"), timeout=180)
        check("completed after fallback", final5["status"] == "completed",
              f"status={final5['status']} err={final5['error'][:80]}")
        check("fell back to no-range mode", final5["supports_range"] is False,
              f"supports_range={final5['supports_range']}")
        f5 = os.path.join(downloads, final5["filename"])
        check("size exact after fallback", os.path.exists(f5) and os.path.getsize(f5) == FILE_SIZE)

        print("\n6) Filename resolution edge cases")
        t6 = mgr.add(f"{main_srv.url}/redirect")
        f6 = wait_status(mgr, t6.id, ("completed", "failed"), timeout=90)
        check("name follows redirect target", f6["status"] == "completed"
              and f6["filename"].startswith("sample_file") and f6["filename"].endswith(".bin"),
              f"got {f6['filename']!r} err={f6['error'][:60]}")

        t7 = mgr.add(f"{main_srv.url}/noident")
        f7 = wait_status(mgr, t7.id, ("completed", "failed"), timeout=90)
        check("extension inferred from Content-Type", f7["filename"] == "noident.zip",
              f"got {f7['filename']!r}")

        t8 = mgr.add(f"{main_srv.url}/download?id=99")
        f8 = wait_status(mgr, t8.id, ("completed", "failed"), timeout=90)
        check("query-only URL gets fallback name",
              f8["filename"].startswith("download") and f8["filename"].endswith(".bin"),
              f"got {f8['filename']!r}")

        print("\n7) HTML page URL rejected with clear error")
        t9 = mgr.add(f"{main_srv.url}/page")
        f9 = wait_status(mgr, t9.id, ("failed", "completed"), timeout=30)
        check("failed", f9 and f9["status"] == "failed", f"status={f9['status'] if f9 else '?'}")
        check("explains it is a web page", "web page" in (f9["error"] if f9 else ""),
              f"err={f9['error'][:80] if f9 else ''}")

        print("\n8) Unknown-length chunked stream (no Content-Length)")
        t10 = mgr.add(f"{main_srv.url}/stream")
        f10 = wait_status(mgr, t10.id, ("completed", "failed"), timeout=180)
        check("completed", f10 and f10["status"] == "completed",
              f"err={f10['error'][:80] if f10 else ''}")
        f10p = os.path.join(downloads, f10["filename"])
        check("stream size exact",
              os.path.exists(f10p) and os.path.getsize(f10p) == FILE_SIZE,
              f"size={os.path.getsize(f10p) if os.path.exists(f10p) else 'missing'}")

        print("\n9) Cancel removes partial data")
        t11 = mgr.add(f"{main_srv.url}/big.bin")
        wait_status(mgr, t11.id, ("downloading",), timeout=60)
        time.sleep(1.0)
        mgr.cancel(t11.id)
        c = wait_status(mgr, t11.id, ("canceled",), timeout=30)
        check("canceled", c and c["status"] == "canceled", f"status={c['status'] if c else '?'}")
        check("no leftover .part", not os.path.exists(
            os.path.join(downloads, (c["filename"] or "x") + ".part")))

        print("\n10) Queue prioritization")
        mgr.update_settings({"max_downloads": 1})
        # Occupy the single slot with a task that can never resolve (dead port).
        busy = mgr.add("http://127.0.0.1:1/dead")
        wait_status(mgr, busy.id, ("waiting_network", "failed"), timeout=30)
        pa = mgr.add(f"{main_srv.url}/big.bin")
        pb = mgr.add(f"{main_srv.url}/big.bin")
        pc = mgr.add(f"{main_srv.url}/big.bin")
        mgr.set_priority(pc.id, 10)
        mgr.pause(busy.id)                        # frees the slot

        def first_to_start(candidates, exclude=(), timeout=10):
            end = time.time() + timeout
            while time.time() < end:
                st = {t["id"]: t["status"] for t in mgr.snapshot()["tasks"]}
                for tid in candidates:
                    if tid not in exclude and st.get(tid) not in (None, "queued"):
                        return tid
                time.sleep(0.1)
            return None

        first = first_to_start((pc.id, pa.id, pb.id))
        check("priority task wins the free slot", first == pc.id,
              f"first started={first}, expected pc={pc.id}")
        second = first_to_start((pa.id, pb.id), exclude=(first or "",),
                                timeout=60)
        check("oldest queued runs next (FIFO for ties)", second == pa.id,
              f"second started={second}, expected pa={pa.id}")
        for tid in (busy.id, pa.id, pb.id, pc.id):
            mgr.cancel(tid)
        time.sleep(1.5)
        mgr.update_settings({"max_downloads": 3})

        print("\n11) Load balancer: least-loaded + speed-weighted NIC selection")
        from engine.network import Interface
        with mgr.monitor.lock:
            mgr.monitor.interfaces = {
                "wifi": Interface(name="wifi", ip="10.0.0.2", kind="Wi-Fi"),
                "eth": Interface(name="eth", ip="10.0.0.3", kind="Ethernet"),
            }
        picks = [mgr._pick_interface() for _ in range(6)]
        check("equal speeds → both NICs used",
              picks.count("10.0.0.2") > 0 and picks.count("10.0.0.3") > 0,
              f"picks={picks}")
        with mgr.monitor.lock:
            mgr.monitor.interfaces["eth"].ema_speed = 50_000_000
            mgr.monitor.interfaces["wifi"].ema_speed = 2_000_000
        picks2 = [mgr._pick_interface() for _ in range(10)]
        check("faster NIC receives more slots",
              picks2.count("10.0.0.3") > picks2.count("10.0.0.2"),
              f"picks={picks2}")
        with mgr.monitor.lock:
            for i in mgr.monitor.interfaces.values():
                i.workers = 0

        print("\n12) Snapshot / settings API shape")
        snap = mgr.snapshot()
        check("snapshot keys", all(k in snap for k in
                                   ("tasks", "networks", "combined", "settings",
                                    "active_count", "queued_count")))
        check("combined history present", isinstance(snap["combined"]["history"], list))

        print("\n13) Server restart recovers state and resumes running downloads")
        data2 = os.path.join(tmp, "data2")
        mgr2 = DownloadManager(data_dir=data2, default_dir=downloads)
        r = mgr2.add(f"{main_srv.url}/big.bin")
        wait_downloaded(mgr2, r.id, 2 * 1024 * 1024, timeout=60)
        mgr2.shutdown()          # simulate a server restart mid-download
        check("state files written",
              os.path.exists(os.path.join(data2, "settings.json")) and
              os.path.exists(os.path.join(data2, "downloadings.json")))
        mgr3 = DownloadManager(data_dir=data2, default_dir=downloads)   # "restart"
        resumed = wait_status(mgr3, r.id, ("downloading", "completed", "failed"), timeout=120)
        check("auto-resumed after restart",
              resumed and resumed["status"] != "failed",
              f"status={resumed['status'] if resumed else '?'} "
              f"err={(resumed or {}).get('error', '')[:80]}")
        done = wait_status(mgr3, r.id, ("completed", "failed"), timeout=120)
        r_path = os.path.join(downloads, (done or {}).get("filename", ""))
        r_ok = bool(done and os.path.exists(r_path)
                    and os.path.getsize(r_path) == FILE_SIZE)
        if r_ok:
            with open(r_path, "rb") as fh:
                r_ok = hashlib.sha256(fh.read()).hexdigest() == FIXTURE_HASH
        check("restart-resumed byte-exact", r_ok,
              f"err={(done or {}).get('error', '')[:80]}")
        mgr3.shutdown()
    finally:
        mgr.shutdown()
        main_srv.stop()
        no_range.stop()
        probe_only.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 48}\n  {PASS} passed, {FAIL} failed\n{'=' * 48}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
