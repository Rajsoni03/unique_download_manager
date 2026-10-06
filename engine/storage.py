"""Filename resolution, HTTP metadata probing and on-disk state persistence."""

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import unquote, urlsplit

import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 UniqueDownloadManager/1.0"

CHUNK_MIN = 4 * 1024 * 1024
CHUNK_MAX = 16 * 1024 * 1024
CHUNK_CAP = 64

_CT_EXT = {
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "application/gzip": ".gz",
    "application/x-gzip": ".gz",
    "application/x-tar": ".tar",
    "application/x-bzip2": ".bz2",
    "application/x-7z-compressed": ".7z",
    "application/x-rar-compressed": ".rar",
    "application/pdf": ".pdf",
    "application/json": ".json",
    "application/xml": ".xml",
    "application/xhtml+xml": ".xhtml",
    "application/csv": ".csv",
    "application/octet-stream": ".bin",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.apple.installer+xml": ".pkg",
    "application/x-msdownload": ".exe",
    "application/x-msdos-program": ".exe",
    "application/iso9660-image": ".iso",
    "application/x-iso9660-image": ".iso",
    "application/java-archive": ".jar",
    "application/wasm": ".wasm",
    "application/epub+zip": ".epub",
    "text/plain": ".txt",
    "text/html": ".html",
    "text/css": ".css",
    "text/csv": ".csv",
    "text/xml": ".xml",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/quicktime": ".mov",
    "application/x-mpegURL": ".m3u8",
}


class FetchError(Exception):
    """Raised when a URL cannot be turned into a downloadable file.

    `transient=True` means the problem is likely temporary (offline, timeout)
    and the engine should wait and retry; `False` means it is fatal.
    """

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@dataclass
class MetaResult:
    final_url: str
    status: int
    total: Optional[int]
    supports_range: bool
    etag: str = ""
    last_modified: str = ""
    content_type: str = ""
    content_disposition: str = ""


# --------------------------------------------------------------------------- naming

def parse_content_disposition(header: str) -> str:
    if not header:
        return ""
    # RFC 5987: filename*=UTF-8''percent-encoded
    m = re.search(r"filename\*\s*=\s*([^']*)''([^;]+)", header, flags=re.I)
    if m:
        return unquote(m.group(2).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"', header, flags=re.I)
    if m:
        return m.group(1).replace('\\"', '"').strip()
    m = re.search(r"filename\s*=\s*([^;]+)", header, flags=re.I)
    if m:
        return m.group(1).strip().strip('"')
    return ""


def sanitize_filename(name: str, fallback_ext: str = "") -> str:
    name = unquote(name or "").strip()
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(". ")
    if not name or name in (".", ".."):
        return ""
    root, ext = os.path.splitext(name)
    if not root:
        return ""
    if fallback_ext and not ext:
        ext = fallback_ext
    # cap length while keeping the extension
    if len(root) > 180:
        root = root[:180]
    return (root + ext)[:220]


def ext_from_content_type(ct: str) -> str:
    if not ct:
        return ""
    ct = ct.split(";")[0].strip().lower()
    return _CT_EXT.get(ct, "")


def unique_path(directory: str, filename: str) -> str:
    """Append ' (1)', ' (2)'… until neither file, .part nor meta exists."""
    base, ext = os.path.splitext(filename)
    candidate = filename
    n = 0
    while True:
        path = os.path.join(directory, candidate)
        if not (os.path.exists(path) or os.path.exists(path + ".part")):
            return candidate
        n += 1
        candidate = f"{base} ({n}){ext}"


def build_filename(meta: MetaResult, original_url: str) -> str:
    name = parse_content_disposition(meta.content_disposition)
    if not name:
        for u in (meta.final_url, original_url):
            path = urlsplit(u).path
            base = os.path.basename(unquote(path))
            if base and base not in (".", "..") and "/" not in base:
                name = base
                break
    ct = meta.content_type
    ext = ext_from_content_type(ct)
    if not name:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        name = f"download_{stamp}{ext}"
    sanitized = sanitize_filename(name, fallback_ext=ext)
    if not sanitized:
        sanitized = f"download_{time.strftime('%Y%m%d_%H%M%S')}{ext}"
    return sanitized


# --------------------------------------------------------------------------- probing

def resolve_meta(url: str, timeout: float = 25.0) -> MetaResult:
    """Probe a URL: follow redirects, detect range support, size, headers.

    Uses a 1-byte range GET (works on servers that reject HEAD).
    Raises FetchError for unusable URLs (HTML pages, HTTP errors…).
    """
    headers = {"User-Agent": UA, "Accept": "*/*"}
    try:
        resp = requests.get(
            url, headers={**headers, "Range": "bytes=0-0"},
            stream=True, timeout=(10, timeout), allow_redirects=True,
        )
    except requests.exceptions.MissingSchema as exc:
        raise FetchError(f"Invalid URL: {exc}")
    except requests.exceptions.InvalidURL:
        raise FetchError("Invalid URL")
    except requests.exceptions.TooManyRedirects:
        raise FetchError("Too many redirects while resolving the URL")
    except requests.exceptions.SSLError as exc:
        raise FetchError(f"SSL error: {exc}")
    except requests.exceptions.ConnectionError as exc:
        raise FetchError(f"Could not connect: {exc}", transient=True)
    except requests.exceptions.Timeout:
        raise FetchError("Connection timed out while resolving the URL", transient=True)

    try:
        status = resp.status_code
        if status not in (200, 206, 416):
            transient = status in (408, 425, 429) or status >= 500
            raise FetchError(f"Server returned HTTP {status}", transient=transient)

        final_url = resp.url
        ct = (resp.headers.get("Content-Type") or "").strip()
        cd = (resp.headers.get("Content-Disposition") or "").strip()
        etag = (resp.headers.get("ETag") or "").strip()
        lm = (resp.headers.get("Last-Modified") or "").strip()

        total: Optional[int] = None
        supports_range = False
        if status == 206:
            supports_range = True
            cr = resp.headers.get("Content-Range", "")
            m = re.match(r"bytes\s+\d+-\d+/(\d+|\*)", cr)
            if m and m.group(1) != "*":
                total = int(m.group(1))
        elif status == 200:
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                total = int(cl)
        elif status == 416:
            total = 0
            supports_range = True

        # Decide whether this is actually a file or an HTML page.
        has_disposition = bool(cd)
        if not has_disposition and ("html" in ct.lower() or not ct):
            try:
                peek = next(resp.iter_content(1024), b"") or b""
            except Exception:
                peek = b""
            head = peek.lstrip()[:200].lower()
            if head.startswith(b"<!doctype html") or head.startswith(b"<html") or head.startswith(b"<head"):
                raise FetchError(
                    "This URL returned a web page instead of a file "
                    "(the link may need login, expired, or points to a landing page)."
                )

        return MetaResult(
            final_url=final_url, status=status, total=total,
            supports_range=supports_range, etag=etag, last_modified=lm,
            content_type=ct, content_disposition=cd,
        )
    finally:
        try:
            resp.close()
        except Exception:
            pass


def revalidate(url: str, total: Optional[int], etag: str, last_modified: str,
               timeout: float = 15.0) -> bool:
    """Return True if the remote file still matches our saved resume metadata."""
    try:
        meta = resolve_meta(url, timeout=timeout)
    except FetchError:
        raise
    except Exception:
        # Transient failure — assume unchanged; retry logic will handle connectivity.
        return True
    if meta.status == 416:
        return False
    if etag and meta.etag and etag != meta.etag:
        return False
    if last_modified and meta.last_modified and last_modified != meta.last_modified:
        return False
    if meta.total is not None and total is not None and meta.total != total:
        return False
    if not etag and not last_modified and total is None:
        return False   # nothing to compare — safest to restart
    return True


# --------------------------------------------------------------------------- chunks

def calc_chunk_size(total: Optional[int], connections: int) -> int:
    if total is None or total < 1_500_000:
        return 0
    target = total // max(1, int(connections * 1.5))
    return max(CHUNK_MIN, min(CHUNK_MAX, target))


def make_chunks(total: Optional[int], chunk_size: int) -> list:
    from .task import Chunk
    if total is None:
        return [Chunk(index=0, start=0, end=None)]
    if total <= 0:
        return []
    if chunk_size <= 0 or total <= chunk_size:
        return [Chunk(index=0, start=0, end=total - 1)]
    n = min(CHUNK_CAP, math.ceil(total / chunk_size))
    size = math.ceil(total / n)
    chunks = []
    start = 0
    idx = 0
    while start < total:
        end = min(total - 1, start + size - 1)
        chunks.append(Chunk(index=idx, start=start, end=end))
        idx += 1
        start = end + 1
    return chunks


# ---------------------------------------------------------------------- persistence

def meta_path(directory: str, task_id: str) -> str:
    return os.path.join(directory, ".udm", f"{task_id}.json")


def part_path(directory: str, filename: str) -> str:
    return os.path.join(directory, filename + ".part")


def ensure_dir(directory: str) -> None:
    os.makedirs(os.path.join(directory, ".udm"), exist_ok=True)


def save_meta(directory: str, task) -> None:
    try:
        ensure_dir(directory)
        data = {
            "task_id": task.id,
            "url": task.url,
            "final_url": task.final_url,
            "filename": task.filename,
            "total": task.total,
            "etag": task.etag,
            "last_modified": task.last_modified,
            "supports_range": task.supports_range,
            "content_type": task.content_type,
            "chunk_size": task.chunk_size,
            "priority": task.priority,
            "created": task.created,
            "chunks": [
                {"index": c.index, "start": c.start, "end": c.end,
                 "done": c.done, "finished": c.finished}
                for c in task.chunks
            ],
        }
        path = meta_path(directory, task.id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        pass


def load_meta(directory: str, task_id: str) -> Optional[dict]:
    path = meta_path(directory, task_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def delete_meta(directory: str, task_id: str) -> None:
    try:
        os.remove(meta_path(directory, task_id))
    except OSError:
        pass


def save_state(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
