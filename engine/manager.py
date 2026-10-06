"""Download manager: queueing, scheduling, load balancing, persistence, snapshots."""

import math
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional

from . import downloader, network, storage
from .downloader import chunk_worker
from .network import NetworkMonitor
from .storage import FetchError, build_filename, calc_chunk_size, make_chunks, unique_path
from .task import Chunk, State, Task


class Settings:
    def __init__(self, data_dir: str, default_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "settings.json")
        self.directory = default_dir
        self.max_downloads = 2
        self.connections = 8
        self.enabled_ifaces: Optional[List[str]] = None   # None = auto (all)
        raw = storage.load_state(self.path)
        if raw:
            self.directory = raw.get("directory", self.directory)
            self.max_downloads = int(raw.get("max_downloads", 2))
            self.connections = max(1, min(32, int(raw.get("connections", 8))))
            ei = raw.get("enabled_ifaces")
            self.enabled_ifaces = ei if isinstance(ei, list) else None

    def save(self) -> None:
        storage.save_state(self.path, {
            "directory": self.directory,
            "max_downloads": self.max_downloads,
            "connections": self.connections,
            "enabled_ifaces": self.enabled_ifaces,
        })


class DownloadManager:
    def __init__(self, data_dir: str, default_dir: Optional[str] = None):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.settings = Settings(data_dir, default_dir or default_download_dir())
        os.makedirs(self.settings.directory, exist_ok=True)

        self.monitor = NetworkMonitor()
        self._apply_iface_settings()

        self.lock = threading.RLock()
        self.tasks: Dict[str, Task] = {}
        self.reserved_paths: Dict[str, str] = {}   # lower(path) -> task_id
        self._state_dirty = False
        self._running = True

        self._restore_state()

        self._threads = [
            threading.Thread(target=self._scheduler_loop, daemon=True, name="udm-scheduler"),
            threading.Thread(target=self._sampler_loop, daemon=True, name="udm-sampler"),
            threading.Thread(target=self._saver_loop, daemon=True, name="udm-saver"),
        ]
        for t in self._threads:
            t.start()

    # ------------------------------------------------------------------ lifecycle

    def shutdown(self) -> None:
        self._running = False
        with self.lock:
            active = [t for t in self.tasks.values() if t.status in State.ACTIVE]
        for t in active:
            t.pause.set()
        # Persist whatever we have right now.
        self._persist_state()
        for t in self.tasks.values():
            if t.filename and t.chunks:
                storage.save_meta(t.directory, t)

    # ------------------------------------------------------------------ actions

    def add(self, url: str, directory: Optional[str] = None, priority: int = 0) -> Task:
        url = (url or "").strip()
        if not url:
            raise ValueError("URL is empty")
        if len(url) > 8192:
            raise ValueError("URL is too long")
        if "://" not in url:
            url = "https://" + url
        scheme = url.split("://", 1)[0].lower()
        if scheme not in ("http", "https"):
            raise ValueError("Only http:// and https:// URLs are supported")

        target = directory or self.settings.directory
        if not os.path.isdir(target):
            raise ValueError(f"Directory does not exist: {target}")

        task = Task(url=url, directory=target, priority=int(priority))
        with self.lock:
            self.tasks[task.id] = task
            self._state_dirty = True
        return task

    def pause(self, task_id: str) -> None:
        task = self._get(task_id)
        if task.status in (State.QUEUED, State.RESOLVING, State.DOWNLOADING, State.WAITING):
            task.pause.set()
            task.status = State.PAUSED
            task.speed = 0.0
            task.connections_active = 0
            self._state_dirty = True

    def resume(self, task_id: str) -> None:
        task = self._get(task_id)
        if task.status == State.COMPLETED:
            return
        if task.status == State.DOWNLOADING:
            return
        # If the previous run is still winding down, the scheduler will pick it up later.
        task.pause.clear()
        task.fail.clear()
        task.range_broken.clear()
        task.run_gen += 1   # stale workers stop themselves even though pause is clear
        task.error = ""
        with task.chunk_lock:
            for c in task.chunks:
                c.attempts = 0
                c.claimed = False
        task.status = State.QUEUED
        self._state_dirty = True

    def cancel(self, task_id: str) -> None:
        task = self._get(task_id)
        if task.status in State.FINISHED:
            return
        task.cancel.set()
        task.pause.clear()
        if task.thread is None or not task.thread.is_alive():
            self._cleanup_files(task)
            task.status = State.CANCELED
            task.speed = 0.0
            self._state_dirty = True

    def remove(self, task_id: str) -> None:
        task = self._get(task_id)
        if task.status not in State.FINISHED:
            self.cancel(task_id)
        with self.lock:
            self.tasks.pop(task_id, None)
            for key, tid in list(self.reserved_paths.items()):
                if tid == task_id:
                    self.reserved_paths.pop(key, None)
            self._state_dirty = True

    def set_priority(self, task_id: str, delta: int) -> None:
        task = self._get(task_id)
        task.priority = int(task.priority) + int(delta)
        self._state_dirty = True

    def update_settings(self, data: dict) -> None:
        s = self.settings
        directory = data.get("directory")
        if directory:
            directory = os.path.expanduser(str(directory))
            if not os.path.isdir(directory):
                raise ValueError(f"Directory does not exist: {directory}")
            if not os.access(directory, os.W_OK):
                raise ValueError(f"Directory is not writable: {directory}")
            s.directory = directory
        if "max_downloads" in data:
            s.max_downloads = max(1, min(8, int(data["max_downloads"])))
        if "connections" in data:
            s.connections = max(1, min(32, int(data["connections"])))
        if "enabled_ifaces" in data and isinstance(data["enabled_ifaces"], list):
            s.enabled_ifaces = [str(x) for x in data["enabled_ifaces"]]
        s.save()
        self._apply_iface_settings()
        self._state_dirty = True

    def _apply_iface_settings(self) -> None:
        enabled = self.settings.enabled_ifaces
        with self.monitor.lock:
            for name, iface in self.monitor.interfaces.items():
                if enabled is None:
                    iface.enabled = True
                else:
                    iface.enabled = name in enabled

    # ------------------------------------------------------------------ queries

    def _get(self, task_id: str) -> Task:
        with self.lock:
            task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(f"Unknown task: {task_id}")
        return task

    def snapshot(self) -> dict:
        with self.lock:
            tasks = sorted(
                self.tasks.values(),
                key=lambda t: (-t.priority, t.created),
            )
            task_dicts = [t.to_dict() for t in tasks]
            settings = {
                "directory": self.settings.directory,
                "max_downloads": self.settings.max_downloads,
                "connections": self.settings.connections,
                "enabled_ifaces": self.settings.enabled_ifaces,
            }
        return {
            "tasks": task_dicts,
            "networks": self.monitor.snapshot(),
            "combined": {
                "app_speed": self.monitor.combined_app_speed,
                "system_rx": self.monitor.combined_system_rx,
                "system_tx": self.monitor.combined_system_tx,
                "history": self.monitor.history_snapshot,
            },
            "settings": settings,
            "active_count": sum(1 for t in task_dicts if t["status"] in State.ACTIVE),
            "queued_count": sum(1 for t in task_dicts if t["status"] == State.QUEUED),
            "server_time": time.time(),
        }

    # ------------------------------------------------------------------ scheduler

    def _scheduler_loop(self) -> None:
        while self._running:
            time.sleep(0.5)
            try:
                self._schedule_once()
            except Exception:
                pass

    def _schedule_once(self) -> None:
        with self.lock:
            active = [t for t in self.tasks.values() if t.status in State.ACTIVE]
            free = self.settings.max_downloads - len(active)
            if free <= 0:
                return
            queued = sorted(
                (t for t in self.tasks.values() if t.status == State.QUEUED),
                key=lambda t: (-t.priority, t.created),
            )
            for task in queued[:free]:
                if task.thread is not None and task.thread.is_alive():
                    continue   # previous run still winding down
                task.status = State.RESOLVING
                thread = threading.Thread(
                    target=self._run_task, args=(task,),
                    daemon=True, name=f"udm-task-{task.id}",
                )
                task.thread = thread
                thread.start()
            self._state_dirty = True

    # ------------------------------------------------------------------ task run

    def _run_task(self, task: Task) -> None:
        workers: List[threading.Thread] = []
        task.run_gen += 1   # invalidate any straggler workers from a previous run
        try:
            if not task.meta_resolved:
                if not self._resolve_with_retries(task):
                    self._stop_cleanup(task)
                    return

            if not self._prepare_storage(task):
                self._stop_cleanup(task)
                return

            while True:
                if task.cancel.is_set():
                    self._join(workers)
                    self._cleanup_files(task)
                    task.status = State.CANCELED
                    task.speed = 0.0
                    task.connections_active = 0
                    return
                if task.pause.is_set():
                    self._join(workers)
                    storage.save_meta(task.directory, task)
                    # Re-check: resume() may have cleared the pause while we were
                    # winding down; never overwrite the status it just set.
                    if task.pause.is_set():
                        task.status = State.PAUSED
                        task.speed = 0.0
                        task.connections_active = 0
                        self._state_dirty = True
                    return
                if task.fail.is_set():
                    self._join(workers)
                    storage.save_meta(task.directory, task)
                    if task.fail.is_set():
                        task.status = State.FAILED
                        task.speed = 0.0
                        task.connections_active = 0
                        self._state_dirty = True
                    return

                if task.range_broken.is_set():
                    # Server refused ranges mid-download → restart single-connection.
                    self._join(workers)
                    task.run_gen += 1   # stale workers must not write after reset
                    workers = []
                    task.range_broken.clear()
                    self._reset_to_single_connection(task)
                    continue

                workers = [w for w in workers if w.is_alive()]
                task.connections_active = len(workers)

                if task.status in (State.RESOLVING, State.PAUSED):
                    task.status = State.DOWNLOADING
                    self._state_dirty = True

                # Everything finished?
                if task.chunks and all(c.finished for c in task.chunks) and not workers:
                    self._finalize(task)
                    return

                if not task.chunks:   # zero-byte file
                    self._finalize(task)
                    return

                # Spawn more workers if the task budget and NICs allow it.
                spawned = False
                while len(workers) < self.settings.connections:
                    chunk = self._claim_chunk(task)
                    if chunk is None:
                        break
                    ip = self._pick_interface()
                    if ip is None:
                        with task.chunk_lock:
                            chunk.claimed = False
                        break
                    if not workers and task.status != State.WAITING:
                        task.status = State.DOWNLOADING
                    gen = task.run_gen
                    w = threading.Thread(
                        target=self._run_worker, args=(task, chunk, ip, gen),
                        daemon=True, name=f"udm-chunk-{task.id}-{chunk.index}",
                    )
                    workers.append(w)
                    w.start()
                    spawned = True

                if not workers and not spawned:
                    # No interface available right now → wait for connectivity.
                    if self._usable_ips():
                        # All chunks claimed elsewhere; nothing to do but wait.
                        pass
                    elif task.status != State.WAITING:
                        task.status = State.WAITING
                        task.error = "No active network interface available"

                time.sleep(0.25)

        except FetchError as exc:
            self._fail(task, str(exc))
        except Exception as exc:  # noqa: BLE001 — never let the thread die silently
            self._fail(task, f"Internal error: {exc}")
        finally:
            task.connections_active = 0

    def _stop_cleanup(self, task: Task) -> None:
        """If a run stopped because of cancel, remove partial files/reservation."""
        if task.cancel.is_set():
            self._cleanup_files(task)
            task.status = State.CANCELED
            task.speed = 0.0

    def _resolve_with_retries(self, task: Task) -> bool:
        """Resolve remote metadata; waits/retries while offline. False = stopped."""
        attempt = 0
        while True:
            if task.cancel.is_set():
                task.status = State.CANCELED
                return False
            if task.pause.is_set():
                task.status = State.PAUSED
                return False
            try:
                meta = storage.resolve_meta(task.url)
                task.final_url = meta.final_url
                task.total = meta.total
                task.supports_range = meta.supports_range
                task.etag = meta.etag
                task.last_modified = meta.last_modified
                task.content_type = meta.content_type

                filename = build_filename(meta, task.url)
                self._reserve_filename(task, filename)

                task.meta_resolved = True
                task.error = ""
                self._state_dirty = True
                return True
            except FetchError as exc:
                if not exc.transient:
                    self._fail(task, str(exc))
                    return False
                attempt += 1
                task.status = State.WAITING
                task.error = str(exc)
                delay = min(15.0, 1.5 ** min(attempt, 8))
                end = time.time() + delay
                while time.time() < end:
                    if task.pause.is_set():
                        task.status = State.PAUSED
                        return False
                    if task.cancel.is_set():
                        task.status = State.CANCELED
                        return False
                    time.sleep(0.2)

    def _reserve_filename(self, task: Task, filename: str) -> None:
        with self.lock:
            # Never take a name another task is already using.
            taken = {
                os.path.join(t.directory, t.filename).lower()
                for t in self.tasks.values()
                if t.filename and t.id != task.id and t.status not in (State.CANCELED,)
            }
            while True:
                path = os.path.join(task.directory, filename).lower()
                if path not in taken and path not in self.reserved_paths:
                    break
                base, ext = os.path.splitext(filename)
                n = 1
                candidate = f"{base} ({n}){ext}"
                while os.path.join(task.directory, candidate).lower() in taken or \
                        os.path.join(task.directory, candidate).lower() in self.reserved_paths:
                    n += 1
                    candidate = f"{base} ({n}){ext}"
                filename = candidate
            filename = unique_path(task.directory, filename)
            task.filename = filename
            self.reserved_paths[os.path.join(task.directory, filename).lower()] = task.id

    def _prepare_storage(self, task: Task) -> bool:
        """Create/restore .part + metadata. False = task stopped (pause/cancel)."""
        storage.ensure_dir(task.directory)
        part = storage.part_path(task.directory, task.filename)

        # ---- try to resume an existing partial download
        if task.supports_range and os.path.exists(part):
            meta = storage.load_meta(directory=task.directory, task_id=task.id)
            if meta and meta.get("filename") == task.filename:
                attempt = 0
                while True:
                    if task.pause.is_set():
                        task.status = State.PAUSED
                        return False
                    if task.cancel.is_set():
                        task.status = State.CANCELED
                        return False
                    try:
                        if storage.revalidate(task.url, meta.get("total"),
                                              meta.get("etag", ""),
                                              meta.get("last_modified", "")):
                            self._restore_chunks(task, meta)
                            task.status = State.DOWNLOADING
                            return True
                        break   # remote changed → wipe and restart
                    except FetchError as exc:
                        if not exc.transient:
                            self._fail(task, str(exc))
                            return False
                        attempt += 1
                        task.error = str(exc)
                        if attempt >= 3:
                            # Server unreachable/rate-limited: resume best-effort.
                            # If-Range still protects us — a mismatched server
                            # answers 200 and the download restarts cleanly.
                            self._restore_chunks(task, meta)
                            task.status = State.DOWNLOADING
                            return True
                        task.status = State.WAITING
                        time.sleep(min(15.0, 1.5 ** min(attempt, 8)))

        # ---- fresh start
        self._reset_storage(task)
        task.status = State.DOWNLOADING
        return True

    def _restore_chunks(self, task: Task, meta: dict) -> None:
        chunks = []
        for c in meta.get("chunks", []):
            chunks.append(Chunk(
                index=int(c["index"]), start=int(c["start"]),
                end=c.get("end"), done=int(c.get("done", 0)),
                finished=bool(c.get("finished", False)),
            ))
        task.chunks = chunks
        if task.total is None and meta.get("total") is not None:
            task.total = meta["total"]
        task.chunk_size = int(meta.get("chunk_size", 0))
        # Sanity: the .part file must be big enough for what the meta claims.
        try:
            size = os.path.getsize(storage.part_path(task.directory, task.filename))
        except OSError:
            size = -1
        needed = max((c.start + c.done for c in chunks), default=0)
        if size < needed:
            # Truncated/corrupt partial → restart cleanly.
            task.chunks = []
            self._reset_storage(task)

    def _reset_storage(self, task: Task) -> None:
        part = storage.part_path(task.directory, task.filename)
        storage.delete_meta(task.directory, task.id)
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
        if not task.supports_range:
            # Server ignores Range → a single sequential connection only;
            # several workers would each write the whole body at wrong offsets.
            task.chunk_size = 0
            task.chunks = make_chunks(task.total, 0)
        else:
            task.chunk_size = calc_chunk_size(task.total, self.settings.connections)
            task.chunks = make_chunks(task.total, task.chunk_size)
        storage.ensure_dir(task.directory)
        with open(part, "wb") as f:
            if task.total:
                f.truncate(task.total)
        storage.save_meta(task.directory, task)

    def _reset_to_single_connection(self, task: Task) -> None:
        task.supports_range = False
        with task.chunk_lock:
            for c in task.chunks:
                c.claimed = False
        task.chunks = []
        task.total = None   # size will be learned from the plain 200 stream
        task.chunk_size = 0
        self._reset_storage(task)

    def _claim_chunk(self, task: Task) -> Optional[Chunk]:
        with task.chunk_lock:
            for c in task.chunks:
                if not c.claimed and not c.finished:
                    c.claimed = True
                    return c
        return None

    def _run_worker(self, task: Task, chunk: Chunk, ip: str, gen: int) -> None:
        try:
            chunk_worker(self, task, chunk, ip, gen)
        finally:
            with self.monitor.lock:
                for iface in self.monitor.interfaces.values():
                    if iface.ip == ip:
                        iface.workers = max(0, iface.workers - 1)
                        break

    # -------------------------------------------------------------- load balance

    def _usable_ips(self) -> List[network.Interface]:
        return self.monitor.usable()

    def _pick_interface(self) -> Optional[str]:
        """Pick the least-loaded enabled NIC, weighted by measured throughput."""
        usable = self._usable_ips()
        if not usable:
            return None
        base = self.settings.connections
        best = None
        best_key = None
        for iface in usable:
            cap = self.monitor.capacity(iface, base)
            ratio = iface.workers / max(1, cap)
            # lower load ratio wins; tie-break on speed then name
            key = (ratio, -iface.ema_speed, -iface.speed_mbps, iface.name)
            if best_key is None or key < best_key:
                best, best_key = iface, key
        if best is None:
            return None
        best.workers += 1
        return best.ip

    # ------------------------------------------------------------------ finishing

    def _finalize(self, task: Task) -> None:
        part = storage.part_path(task.directory, task.filename)
        final_name = task.filename
        final_path = os.path.join(task.directory, final_name)
        if os.path.exists(final_path):
            final_name = unique_path(task.directory, final_name)
            final_path = os.path.join(task.directory, final_name)
            task.filename = final_name
        if os.path.exists(part):
            os.replace(part, final_path)
        storage.delete_meta(task.directory, task.id)
        task.status = State.COMPLETED
        task.completed_at = time.time()
        task.speed = 0.0
        task.eta = None
        task.connections_active = 0
        task.error = ""
        if task.total is None:
            task.total = task.downloaded
        with self.lock:
            self.reserved_paths.pop(
                os.path.join(task.directory, final_name).lower(), None)
            self._state_dirty = True

    def _fail(self, task: Task, message: str) -> None:
        task.fail.set()
        task.status = State.FAILED
        task.error = message[:500]
        task.speed = 0.0
        task.connections_active = 0
        if task.filename:
            storage.save_meta(task.directory, task)
        self._state_dirty = True

    def _cleanup_files(self, task: Task) -> None:
        if not task.filename:
            return
        try:
            os.remove(storage.part_path(task.directory, task.filename))
        except OSError:
            pass
        storage.delete_meta(task.directory, task.id)
        with self.lock:
            self.reserved_paths.pop(
                os.path.join(task.directory, task.filename).lower(), None)

    @staticmethod
    def _join(threads: List[threading.Thread], timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        for t in threads:
            remaining = max(0.1, deadline - time.time())
            if t.is_alive():
                t.join(timeout=remaining)

    # ------------------------------------------------------------------ loops

    def _sampler_loop(self) -> None:
        while self._running:
            time.sleep(1.0)
            try:
                self.monitor.sample()
                with self.lock:
                    active = [t for t in self.tasks.values() if t.status in State.ACTIVE]
                    all_tasks = list(self.tasks.values())
                for task in active:
                    speed = task.win_total
                    task.win_total = 0
                    task.iface_speed = dict(task.win_iface)
                    task.win_iface = {}
                    task.speed = float(speed)
                    if task.total and task.speed > 0:
                        remaining = max(0, task.total - task.downloaded)
                        task.eta = remaining / task.speed if remaining else 0.0
                    else:
                        task.eta = None
                for task in all_tasks:
                    if task.status not in State.ACTIVE:
                        task.speed = 0.0
                        task.iface_speed = {}
                        task.eta = None
            except Exception:
                pass

    def _saver_loop(self) -> None:
        while self._running:
            time.sleep(3.0)
            try:
                with self.lock:
                    dirty = self._state_dirty
                    self._state_dirty = False
                    active = [t for t in self.tasks.values() if t.status in State.ACTIVE]
                for task in active:
                    if task.filename and task.chunks:
                        with task.chunk_lock:
                            storage.save_meta(task.directory, task)
                if dirty:
                    self._persist_state()
            except Exception:
                pass

    # ------------------------------------------------------------------ state

    def _persist_state(self) -> None:
        with self.lock:
            tasks = []
            for t in self.tasks.values():
                tasks.append({
                    "id": t.id, "url": t.url, "directory": t.directory,
                    "filename": t.filename, "priority": t.priority,
                    "created": t.created, "status": t.status, "error": t.error,
                    "total": t.total, "final_url": t.final_url,
                    "etag": t.etag, "last_modified": t.last_modified,
                    "supports_range": t.supports_range,
                    "content_type": t.content_type,
                    "chunk_size": t.chunk_size,
                    "completed_at": t.completed_at,
                })
            settings = {
                "directory": self.settings.directory,
                "max_downloads": self.settings.max_downloads,
                "connections": self.settings.connections,
                "enabled_ifaces": self.settings.enabled_ifaces,
            }
        storage.save_state(os.path.join(self.data_dir, "state.json"),
                           {"tasks": tasks, "settings": settings})

    def _restore_state(self) -> None:
        raw = storage.load_state(os.path.join(self.data_dir, "state.json"))
        for item in raw.get("tasks", []):
            try:
                task = Task(url=item["url"], directory=item["directory"],
                            priority=int(item.get("priority", 0)))
                task.id = item.get("id") or task.id
                task.created = float(item.get("created", time.time()))
                task.filename = item.get("filename", "")
                task.total = item.get("total")
                task.final_url = item.get("final_url", "")
                task.etag = item.get("etag", "")
                task.last_modified = item.get("last_modified", "")
                task.supports_range = bool(item.get("supports_range"))
                task.content_type = item.get("content_type", "")
                task.chunk_size = int(item.get("chunk_size", 0))
                task.completed_at = item.get("completed_at")
                task.meta_resolved = bool(task.final_url)

                status = item.get("status", State.QUEUED)
                if status in (State.COMPLETED,):
                    task.status = State.COMPLETED
                elif status == State.CANCELED:
                    task.status = State.CANCELED
                elif status == State.FAILED:
                    task.status = State.FAILED
                    task.error = item.get("error", "")
                else:
                    part = storage.part_path(task.directory, task.filename) if task.filename else ""
                    meta = storage.load_meta(task.directory, task.id) if task.filename else None
                    if task.filename and meta and os.path.exists(part):
                        self._restore_chunks(task, meta)
                        task.status = State.PAUSED
                    else:
                        task.status = State.QUEUED
                        task.meta_resolved = False
                self.tasks[task.id] = task
            except Exception:
                continue

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def reveal(path: str) -> None:
        """Reveal a file/folder in the OS file manager."""
        target = path
        if sys.platform == "darwin":
            if os.path.isdir(target):
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["open", "-R", target])
        elif os.name == "nt":
            if os.path.isdir(target):
                os.startfile(target)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["explorer", "/select,", target])
        else:
            folder = target if os.path.isdir(target) else os.path.dirname(target)
            subprocess.Popen(["xdg-open", folder])


def default_download_dir() -> str:
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    if os.path.isdir(downloads):
        return downloads
    return os.path.expanduser("~")
