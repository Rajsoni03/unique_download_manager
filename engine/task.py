"""Task and chunk data models."""

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class State:
    QUEUED = "queued"
    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    WAITING = "waiting_network"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"

    ACTIVE = {RESOLVING, DOWNLOADING, WAITING}
    FINISHED = {COMPLETED, FAILED, CANCELED}


@dataclass
class Chunk:
    index: int
    start: int
    end: Optional[int]          # inclusive; None = open-ended (unknown total size)
    done: int = 0
    claimed: bool = False
    finished: bool = False
    attempts: int = 0

    @property
    def size(self) -> Optional[int]:
        if self.end is None:
            return None
        return self.end - self.start + 1

    @property
    def remaining(self) -> Optional[int]:
        if self.end is None:
            return None
        return max(0, self.end - self.start + 1 - self.done)


class Task:
    def __init__(self, url: str, directory: str, priority: int = 0) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.directory = directory
        self.priority = priority
        self.created = time.time()
        self.completed_at: Optional[float] = None
        self.active_seconds = 0.0
        self.active_since: Optional[float] = None
        self.active_lock = threading.Lock()

        self.status = State.QUEUED
        self.error = ""
        self.filename = ""
        self.final_url = ""
        self.content_type = ""
        self.total: Optional[int] = None       # None = unknown length
        self.supports_range = False
        self.etag = ""
        self.last_modified = ""
        self.chunk_size = 0
        self.chunks: List[Chunk] = []

        # progress
        self.speed = 0.0                        # bytes/s (1s window, sampled)
        self.eta: Optional[float] = None
        self.iface_bytes: Dict[str, int] = {}   # ip -> cumulative bytes
        self.iface_speed: Dict[str, int] = {}   # ip -> bytes/s current window
        self.connections_active = 0

        # coordination
        self.pause = threading.Event()
        self.cancel = threading.Event()
        self.fail = threading.Event()
        self.range_broken = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.chunk_lock = threading.Lock()
        self.meta_resolved = False
        # Generation counter: workers capture it at spawn and stop themselves
        # when it no longer matches, so a resumed task never has two live runs.
        self.run_gen = 0

        # sampler windows (manager clears once per second)
        self.win_total = 0
        self.win_iface: Dict[str, int] = {}

    # ------------------------------------------------------------------ helpers

    @property
    def downloaded(self) -> int:
        with self.chunk_lock:
            if self.status == State.COMPLETED and self.total is not None:
                return self.total
            return sum(c.done for c in self.chunks)

    @property
    def finished_chunks(self) -> int:
        return sum(1 for c in self.chunks if c.finished)

    @property
    def is_resumable_file(self) -> bool:
        return bool(self.filename)

    def start_active_clock(self) -> None:
        with self.active_lock:
            if self.active_since is None:
                self.active_since = time.monotonic()

    def stop_active_clock(self) -> None:
        with self.active_lock:
            if self.active_since is not None:
                self.active_seconds += max(0.0, time.monotonic() - self.active_since)
                self.active_since = None

    def reset_active_clock(self) -> None:
        with self.active_lock:
            self.active_seconds = 0.0
            if self.active_since is not None:
                self.active_since = time.monotonic()

    def active_elapsed(self) -> float:
        with self.active_lock:
            elapsed = self.active_seconds
            if self.active_since is not None:
                elapsed += max(0.0, time.monotonic() - self.active_since)
            return elapsed

    def progress(self) -> float:
        if self.status == State.COMPLETED:
            return 100.0
        if not self.total:
            return 0.0
        return min(100.0, self.downloaded * 100.0 / self.total)

    @property
    def average_speed(self) -> float:
        elapsed = self.active_elapsed()
        return self.downloaded / elapsed if elapsed else 0.0

    def to_dict(self) -> dict:
        total = self.total or 0
        return {
            "id": self.id,
            "url": self.url,
            "final_url": self.final_url,
            "filename": self.filename,
            "directory": self.directory,
            "path": os.path.join(self.directory, self.filename) if self.filename else "",
            "status": self.status,
            "error": self.error,
            "total": self.total,
            "downloaded": self.downloaded,
            "progress": self.progress(),
            "speed": self.speed,
            "average_speed": self.average_speed,
            "eta": self.eta,
            "priority": self.priority,
            "created": self.created,
            "completed_at": self.completed_at,
            "supports_range": self.supports_range,
            "connections": self.connections_active,
            "chunks": {
                "total": len(self.chunks),
                "finished": self.finished_chunks,
                "size": self.chunk_size,
            },
            "iface_bytes": dict(self.iface_bytes),
            "iface_speed": dict(self.iface_speed),
            "content_type": self.content_type,
        }
