"""Chunk download workers: parallel, resumable, bound to a specific NIC."""

import os
import re
import threading
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

from .storage import UA
from .task import Chunk, State, Task

CONNECT_TIMEOUT = 15
READ_TIMEOUT = 30
NET_ATTEMPTS = 80        # ~35 min of offline waiting at 30 s cap
HTTP_ATTEMPTS = 5
BACKOFF_CAP = 30.0


class TaskStopped(Exception):
    pass


class RangeUnsupported(Exception):
    pass


class HttpError(Exception):
    def __init__(self, status: int, message: str = ""):
        self.status = status
        super().__init__(message or f"HTTP {status}")


class BoundAdapter(HTTPAdapter):
    """HTTPAdapter that binds outgoing sockets to a chosen local IP."""

    def __init__(self, source_ip: str, **kwargs):
        self._source_ip = source_ip
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        kwargs["source_address"] = (self._source_ip, 0)
        super().init_poolmanager(connections, maxsize, block=block, **kwargs)


def _make_session(ip: str) -> requests.Session:
    s = requests.Session()
    adapter = BoundAdapter(ip, max_retries=0)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Encoding": "identity",   # crucial: ranged bytes must not be transcoded
        "Connection": "keep-alive",
    })
    return s


def _stopped(task: Task, gen: Optional[int] = None) -> bool:
    return (task.pause.is_set() or task.cancel.is_set()
            or task.fail.is_set() or task.range_broken.is_set()
            or (gen is not None and gen != task.run_gen))


def _range_header(task: Task, chunk: Chunk, can_resume: bool) -> Optional[str]:
    if not task.supports_range:
        return None
    start = chunk.start + (chunk.done if can_resume else 0)
    if chunk.end is None:
        return f"bytes={start}-"
    return f"bytes={start}-{chunk.end}"


def _if_range(task: Task) -> dict:
    if task.etag:
        return {"If-Range": task.etag}
    if task.last_modified:
        return {"If-Range": task.last_modified}
    return {}


def _write_pieces(mgr, task: Task, chunk: Chunk, resp, ip: str, expected_start: int,
                  lock: threading.Lock, gen: int) -> None:
    """Stream the response body into the .part file at the right offset."""
    part = os.path.join(task.directory, task.filename + ".part")
    with open(part, "r+b", buffering=0) as f:
        f.seek(expected_start)
        for piece in resp.iter_content(256 * 1024):
            if _stopped(task, gen):
                raise TaskStopped()
            if not piece:
                continue
            view = memoryview(piece)
            while view:
                n = f.write(view)
                if n is None or n == 0:
                    raise OSError("disk write returned 0 bytes")
                view = view[n:]
            size = len(piece)
            with lock:
                chunk.done += size
                task.win_total += size
                task.win_iface[ip] = task.win_iface.get(ip, 0) + size
                task.iface_bytes[ip] = task.iface_bytes.get(ip, 0) + size
            mgr.monitor.add_app_bytes(ip, size)


def _attempt(mgr, task: Task, chunk: Chunk, ip: str, gen: int) -> None:
    """One HTTP attempt for the current chunk. Raises on failure."""
    can_resume = task.supports_range
    if not can_resume:
        # Cannot resume: restart this chunk from its beginning.
        with task.chunk_lock:
            if chunk.done:
                chunk.done = 0

    start = chunk.start + (chunk.done if can_resume else 0)
    if chunk.end is not None and can_resume and start > chunk.end:
        chunk.finished = True
        return

    headers = {}
    rng = _range_header(task, chunk, can_resume)
    if rng:
        headers["Range"] = rng
        headers.update(_if_range(task))

    url = task.final_url or task.url
    session = _make_session(ip)
    try:
        try:
            resp = session.get(url, headers=headers, stream=True,
                               timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                               allow_redirects=True)
        except requests.exceptions.SSLError:
            raise
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            raise ConnectionError(str(exc) or "connection failed") from exc

        try:
            status = resp.status_code
            if status in (408, 425, 429, 500, 502, 503, 504):
                raise HttpError(status)
            if status >= 400:
                if status == 416 and chunk.end is not None and task.total and start >= task.total:
                    chunk.finished = True
                    return
                raise HttpError(status)

            if rng and status == 200:
                # Server ignored our Range request (or file changed) → full restart.
                raise RangeUnsupported()

            expected_start = start
            if status == 206:
                cr = resp.headers.get("Content-Range", "")
                m = re.match(r"bytes\s+(\d+)-", cr)
                if m:
                    got = int(m.group(1))
                    if got != start:
                        raise HttpError(status, f"server returned unexpected range (wanted {start}, got {got})")
                    expected_start = got

            # Mark network as healthy for the task status display.
            if gen == task.run_gen:
                task.error = ""
                if task.status == State.WAITING:
                    task.status = State.DOWNLOADING
            chunk.attempts = 0   # server reachable → consecutive-failure budget resets

            _write_pieces(mgr, task, chunk, resp, ip, expected_start, task.chunk_lock, gen)

            if gen != task.run_gen:
                return   # stale run: don't touch shared state
            if chunk.end is None:
                # Open-ended chunk: stream ended → that's the whole file.
                with task.chunk_lock:
                    if task.total is None:
                        task.total = chunk.done
                chunk.finished = True
            elif chunk.done >= chunk.size:
                chunk.finished = True
            else:
                raise ConnectionError(
                    f"connection closed early ({chunk.done}/{chunk.size} bytes)"
                )
        finally:
            try:
                resp.close()
            except Exception:
                pass
    finally:
        session.close()


def chunk_worker(mgr, task: Task, chunk: Chunk, ip: str, gen: int) -> None:
    """Run by a manager-managed thread; retries with backoff until done/stopped."""
    net_err = True   # whether the previous failure looked like connectivity
    try:
        while not _stopped(task, gen):
            try:
                _attempt(mgr, task, chunk, ip, gen)
                return
            except TaskStopped:
                return
            except RangeUnsupported:
                if gen == task.run_gen:
                    task.range_broken.set()
                return
            except requests.exceptions.SSLError as exc:
                if gen == task.run_gen:
                    task.fail.set()
                    task.error = f"SSL error: {exc}"
                return
            except HttpError as exc:
                if gen != task.run_gen:
                    return
                if 400 <= exc.status < 500 and exc.status not in (408, 425, 429):
                    task.fail.set()
                    task.error = str(exc)
                    return
                net_err = False
                task.error = str(exc)
                chunk.attempts += 1
                if chunk.attempts >= HTTP_ATTEMPTS:
                    task.fail.set()
                    task.error = f"Giving up after {HTTP_ATTEMPTS} attempts: {exc}"
                    return
            except (ConnectionError, OSError, requests.exceptions.RequestException) as exc:
                if gen != task.run_gen:
                    return
                net_err = True
                task.status = State.WAITING
                task.error = str(exc)[:300] or "Network unreachable"
                chunk.attempts += 1
                if chunk.attempts >= NET_ATTEMPTS:
                    task.fail.set()
                    task.error = f"Network unreachable after {NET_ATTEMPTS} retries: {task.error}"
                    return

            # Backoff before the next attempt (unless we're being stopped).
            if net_err:
                delay = min(30.0, 1.5 ** min(chunk.attempts, 10))
            else:
                delay = min(20.0, 2.0 ** min(chunk.attempts, 6))
            end_at = time.time() + delay
            while time.time() < end_at and not _stopped(task, gen):
                time.sleep(0.2)
    finally:
        if not chunk.finished:
            with task.chunk_lock:
                chunk.claimed = False
