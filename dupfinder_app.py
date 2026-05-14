"""Sift — local web UI server.

Run: python dupfinder_app.py
     python dupfinder_app.py --no-browser    (don't auto-open a window)

Starts a tiny HTTP server on 127.0.0.1 and (by default) opens a chromeless
browser window pointed at the UI. All core logic comes from dupfinder.py
(kept under that module name for import compatibility).
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import dupfinder as df


HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Cross-request cancel state. A new scan replaces (and cancels) any prior one.
_current_cancel: threading.Event | None = None
_cancel_lock = threading.Lock()
_scan_jobs: dict[str, dict] = {}
_scan_jobs_lock = threading.Lock()
MAX_SCAN_JOBS = 12


def _begin_scan() -> threading.Event:
    """Register a new cancel event for an in-progress scan."""
    global _current_cancel
    ev = threading.Event()
    with _cancel_lock:
        if _current_cancel is not None:
            _current_cancel.set()  # cancel any prior scan
        _current_cancel = ev
    return ev


def _end_scan(ev: threading.Event) -> None:
    """Clear the cancel event for a finished scan (if still ours)."""
    global _current_cancel
    with _cancel_lock:
        if _current_cancel is ev:
            _current_cancel = None


def _signal_cancel() -> bool:
    """Signal any in-progress scan to stop. Returns True if one was running."""
    with _cancel_lock:
        if _current_cancel is None:
            return False
        _current_cancel.set()
        return True


def _resource_dir() -> Path:
    """Where bundled assets live (frozen exe vs raw script)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


HERE = _resource_dir()
UI_FILE = HERE / "app_ui.html"


def _coerce_path_list(value) -> list[Path]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    out: list[Path] = []
    for raw in value:
        if not raw:
            continue
        out.append(Path(raw))
    return out


def _preferred_keep_index(paths: list[Path], protected_roots: list[Path]) -> int:
    protected = df.protected_indices(paths, protected_roots)
    return min(protected) if protected else 0


def _default_hash_cache_path() -> Path:
    return df.app_data_dir() / df.HASH_CACHE_FILENAME


def _prune_scan_jobs() -> None:
    with _scan_jobs_lock:
        if len(_scan_jobs) <= MAX_SCAN_JOBS:
            return
        ordered = sorted(
            _scan_jobs.items(),
            key=lambda item: item[1].get("updated_at", 0),
        )
        for job_id, job in ordered[:len(_scan_jobs) - MAX_SCAN_JOBS]:
            if job.get("status") != "running":
                _scan_jobs.pop(job_id, None)


def _create_scan_job(cancel: threading.Event) -> str:
    job_id = uuid.uuid4().hex
    now = time.time()
    with _scan_jobs_lock:
        _scan_jobs[job_id] = {
            "job_id": job_id,
            "status": "running",
            "phase": "starting",
            "message": "Starting scan...",
            "files_seen": 0,
            "candidate_files": 0,
            "size_groups": 0,
            "hashed_files": 0,
            "hash_phase": "",
            "cache_enabled": True,
            "cache_hits": 0,
            "cache_misses": 0,
            "cache_writes": 0,
            "cache_errors": 0,
            "result": None,
            "error": None,
            "cancelled": False,
            "created_at": now,
            "updated_at": now,
            "cancel": cancel,
        }
    _prune_scan_jobs()
    return job_id


def _update_scan_job(job_id: str, **updates) -> None:
    with _scan_jobs_lock:
        job = _scan_jobs.get(job_id)
        if job is None:
            return
        job.update(updates)
        job["updated_at"] = time.time()


def _public_scan_job(job_id: str) -> dict | None:
    with _scan_jobs_lock:
        job = _scan_jobs.get(job_id)
        if job is None:
            return None
        out = {k: v for k, v in job.items() if k != "cancel"}
    return out


def _scan_result_payload(
    paths: list[Path],
    skip: list[str],
    min_size: int,
    protected_roots: list[Path],
    cancel: threading.Event,
    progress=None,
    cache_enabled: bool = True,
    cache_path: Path | None = None,
) -> dict:
    cache = df.HashCache(cache_path or _default_hash_cache_path()) if cache_enabled else None
    entries = []
    try:
        for entry in df.iter_files(paths, skip, min_size, cancel=cancel):
            entries.append(entry)
            if progress is not None and len(entries) % 100 == 0:
                progress(
                    phase="walking",
                    message=f"Walking files... {len(entries)} candidate files",
                    files_seen=len(entries),
                    candidate_files=len(entries),
                )
        if progress is not None:
            progress(
                phase="grouping",
                message=f"Grouping {len(entries)} candidate files by size...",
                files_seen=len(entries),
                candidate_files=len(entries),
            )

        size_groups = df.group_by_size(entries)
        if progress is not None:
            progress(
                phase="hashing",
                message=f"Hashing {len(size_groups)} size group(s)...",
                size_groups=len(size_groups),
                cache_enabled=cache_enabled,
            )

        hashed = 0

        def on_hash(_path: Path, partial: bool, _ok: bool, _cached: bool) -> None:
            nonlocal hashed
            hashed += 1
            if progress is not None and (hashed == 1 or hashed % 25 == 0):
                stats = cache.stats() if cache is not None else {}
                progress(
                    phase="hashing",
                    hash_phase="partial" if partial else "full",
                    hashed_files=hashed,
                    message=(
                        f"{'Partial' if partial else 'Full'} hashing..."
                        f" {hashed} hash checks"
                    ),
                    **stats,
                )

        dups = df.find_duplicates(
            size_groups, cancel=cancel, progress=on_hash, cache=cache,
        )

        if progress is not None:
            progress(
                phase="finalizing",
                message=f"Preparing {len(dups)} duplicate group(s)...",
                hashed_files=hashed,
                **(cache.stats() if cache is not None else {}),
            )

        groups_out = []
        for size, paths_in_group in dups:
            df._check_cancel(cancel)
            anchor = df.common_ancestor(paths_in_group)
            shorts = df.short_paths(paths_in_group, anchor)
            hashes = [df.hash_file(p, partial=False, cache=cache, cancel=cancel) for p in paths_in_group]
            valid_hashes = [h for h in hashes if h is not None]
            if len(valid_hashes) != len(paths_in_group) or len(set(valid_hashes)) != 1:
                continue
            files = []
            for p, short, sha256 in zip(paths_in_group, shorts, hashes):
                try:
                    mtime_ts = p.stat().st_mtime
                except OSError:
                    mtime_ts = 0
                files.append({
                    "path": str(p),
                    "short": short,
                    "name": p.name,
                    "mtime": df.format_mtime(p),
                    "mtime_ts": mtime_ts,
                    "sha256": sha256,
                    "protected": df.path_is_under_any(p, protected_roots),
                })
            keep_index = _preferred_keep_index(paths_in_group, protected_roots)
            groups_out.append({
                "size": size,
                "wasted": size * (len(paths_in_group) - 1),
                "anchor": str(anchor) if anchor else None,
                "keep_index": keep_index,
                "protected_count": sum(1 for f in files if f["protected"]),
                "files": files,
            })

        total_wasted = sum(g["wasted"] for g in groups_out)
        total_files = sum(len(g["files"]) for g in groups_out)
        total_recoverable = sum(
            g["size"]
            for g in groups_out
            for i, f in enumerate(g["files"])
            if i != g["keep_index"] and not f["protected"]
        )
        stats = cache.stats() if cache is not None else {
            "cache_hits": 0, "cache_misses": 0, "cache_writes": 0, "cache_errors": 0,
        }
        return {
            "groups": groups_out,
            "total_groups": len(groups_out),
            "total_wasted": total_wasted,
            "total_recoverable": total_recoverable,
            "total_files": total_files,
            "cache_enabled": cache_enabled,
            **stats,
        }
    finally:
        if cache is not None:
            cache.close()


def _run_similar_images_job(
    job_id: str,
    paths: list[Path],
    skip: list[str],
    min_size: int,
    threshold: int,
    cancel: threading.Event,
) -> None:
    """Background worker for /api/scan_similar_start."""

    def progress(stage: str, current: int, total: int) -> None:
        stage_label = {
            "hashing": "Hashing photos",
            "clustering": "Clustering by similarity",
        }.get(stage, stage)
        msg = f"{stage_label}... {current}/{total}" if total else f"{stage_label}..."
        _update_scan_job(
            job_id,
            phase=stage,
            hash_phase="image dhash" if stage == "hashing" else "",
            message=msg,
            candidate_files=total,
            hashed_files=current,
        )

    try:
        groups, scanned, skipped = df.find_similar_images(
            paths, skip,
            threshold=threshold, min_size=min_size,
            cancel=cancel, progress=progress,
        )
    except df.ScanCancelled:
        _update_scan_job(
            job_id,
            status="cancelled", cancelled=True,
            phase="cancelled", message="Scan cancelled.",
            result={"cancelled": True, "pillow_available": True,
                    "groups": [], "total_groups": 0,
                    "scanned": 0, "skipped": 0,
                    "threshold": threshold, "total_wasted": 0},
        )
        return
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc(file=sys.stderr)
        _update_scan_job(
            job_id, status="error", phase="error",
            message=f"{type(e).__name__}: {e}",
            error=f"{type(e).__name__}: {e}",
        )
        return
    finally:
        _end_scan(cancel)

    anchor = df.common_ancestor(paths) if len(paths) > 1 else paths[0]
    groups_out = []
    for g in groups:
        files = []
        for im in g:
            files.append({
                "path": str(im.path),
                "short": df.short_paths([im.path], anchor)[0],
                "size": im.size,
                "size_label": df.format_size(im.size),
                "mtime": df.format_mtime(im.path),
                "mtime_ts": im.mtime_ts,
                "width": im.width,
                "height": im.height,
                "resolution": f"{im.width}x{im.height}" if im.width else "?",
                "thumbnail": im.thumbnail,
            })
        wasted = sum(im.size for im in g[1:])
        groups_out.append({
            "files": files,
            "count": len(files),
            "keep_indices": [0],
            "wasted": wasted,
            "wasted_label": df.format_size(wasted),
        })

    result = {
        "pillow_available": True,
        "groups": groups_out,
        "total_groups": len(groups_out),
        "scanned": scanned,
        "skipped": skipped,
        "threshold": threshold,
        "total_wasted": sum(g["wasted"] for g in groups_out),
    }
    _update_scan_job(
        job_id, status="done", phase="done",
        message=f"Scan complete: {len(groups_out)} similar group(s) in {scanned} image(s).",
        result=result,
    )


def _run_scan_job(
    job_id: str,
    paths: list[Path],
    skip: list[str],
    min_size: int,
    protected_roots: list[Path],
    cancel: threading.Event,
    cache_enabled: bool,
) -> None:
    def progress(**updates) -> None:
        _update_scan_job(job_id, **updates)

    try:
        result = _scan_result_payload(
            paths, skip, min_size, protected_roots, cancel, progress=progress,
            cache_enabled=cache_enabled,
        )
    except df.ScanCancelled:
        _update_scan_job(
            job_id,
            status="cancelled",
            cancelled=True,
            phase="cancelled",
            message="Scan cancelled.",
            result={"cancelled": True, "groups": [], "total_groups": 0,
                    "total_wasted": 0, "total_recoverable": 0, "total_files": 0,
                    "cache_enabled": cache_enabled},
        )
        return
    except Exception as e:  # noqa: BLE001
        _update_scan_job(
            job_id,
            status="error",
            phase="error",
            message=f"{type(e).__name__}: {e}",
            error=f"{type(e).__name__}: {e}",
        )
        return
    finally:
        _end_scan(cancel)

    _update_scan_job(
        job_id,
        status="done",
        phase="done",
        message="Scan complete.",
        result=result,
    )


# =============================================================================
# Native folder / file pickers
#
# We launch Tk in a daemon thread (not a subprocess), which works inside both
# script mode and the PyInstaller-bundled exe. A lock serializes pickers so
# two simultaneous clicks can't fight over the Tk root.
# =============================================================================

_picker_lock = threading.Lock()


def _set_process_dpi_aware() -> None:
    """Make this process HiDPI-aware so the native dialog isn't bitmap-scaled."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def _pick(kind: str) -> str:
    """Show a native folder/file picker and return the chosen path (or '')."""
    if not _picker_lock.acquire(blocking=False):
        return ""  # another picker is open; ignore concurrent click

    import queue
    result: queue.Queue[str] = queue.Queue()

    def worker() -> None:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                if kind == "folder":
                    p = filedialog.askdirectory(parent=root,
                                                title="Pick a folder to scan")
                else:
                    p = filedialog.askopenfilename(
                        parent=root,
                        title="Pick a quarantine manifest",
                        filetypes=[
                            ("Manifest", "manifest.json"),
                            ("JSON", "*.json"),
                            ("All files", "*.*"),
                        ],
                    )
            finally:
                root.destroy()
            result.put(p or "")
        except Exception:  # noqa: BLE001
            result.put("")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        return result.get(timeout=300)
    except Exception:  # noqa: BLE001  — timeout etc.
        return ""
    finally:
        _picker_lock.release()


# =============================================================================
# Request handler
# =============================================================================

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Sift/1.0"

    # ---- helpers ----
    def _read_json(self) -> dict:
        length = int(self.headers.get("content-length", 0))
        if not length:
            return {}
        body = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {}

    def _send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_err(self, msg: str, status: int = 400) -> None:
        self._send_json({"error": msg}, status=status)

    def _send_file(self, path: Path, ctype: str) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(404, "ui file missing")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        # Quiet: only show non-static-asset errors.
        if "404" in format % args or "500" in format % args:
            sys.stderr.write("[server] " + (format % args) + "\n")

    # ---- routes ----
    def do_GET(self) -> None:
        if self.path in ("/", "/index.html", "/app_ui.html"):
            self._send_file(UI_FILE, "text/html; charset=utf-8")
        elif self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    def _csrf_ok(self) -> bool:
        """Reject cross-origin POSTs from random web pages.

        Three layers (any failure rejects):
          1. Host header must point at loopback (defeats DNS rebinding).
          2. Origin header, if present, must match our own URL.
          3. Content-Type must be application/json (this alone forces a
             CORS preflight for cross-origin fetch, which we never answer,
             so cross-origin fetch is blocked at the browser level).
        """
        port = self.server.server_address[1]
        expected_origin = f"http://{HOST}:{port}"

        host = self.headers.get("Host", "")
        host_name = host.split(":")[0].strip().lower()
        if host_name and host_name not in ("127.0.0.1", "localhost"):
            self.send_error(403, "bad host"); return False

        origin = self.headers.get("Origin", "")
        if origin and origin != expected_origin:
            self.send_error(403, "bad origin"); return False
        if origin == "null":
            self.send_error(403, "null origin"); return False

        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self.send_error(415, "json content-type required"); return False

        return True

    def do_POST(self) -> None:
        if not self._csrf_ok():
            return
        try:
            self._dispatch_post()
        except Exception as e:  # noqa: BLE001 — last-resort guard
            import traceback
            traceback.print_exc(file=sys.stderr)
            try:
                self._send_err(f"{type(e).__name__}: {e}", status=500)
            except Exception:
                pass

    def _dispatch_post(self) -> None:
        data = self._read_json()
        if self.path == "/api/browse_folder":
            self._send_json({"path": _pick("folder")})
        elif self.path == "/api/browse_manifest":
            self._send_json({"path": _pick("manifest")})
        elif self.path == "/api/scan":
            self._handle_scan(data)
        elif self.path == "/api/scan_start":
            self._handle_scan_start(data)
        elif self.path == "/api/scan_status":
            self._handle_scan_status(data)
        elif self.path == "/api/scan_empty":
            self._handle_scan_empty(data)
        elif self.path == "/api/scan_largest":
            self._handle_scan_largest(data)
        elif self.path == "/api/scan_similar_images":
            self._handle_scan_similar_images(data)
        elif self.path == "/api/scan_similar_start":
            self._handle_scan_similar_start(data)
        elif self.path == "/api/delete_empty_folders":
            self._handle_delete_empty(data)
        elif self.path == "/api/execute":
            self._handle_execute(data)
        elif self.path == "/api/restore":
            self._handle_restore(data)
        elif self.path == "/api/quarantine_history":
            self._handle_quarantine_history()
        elif self.path == "/api/preview":
            self._handle_preview(data)
        elif self.path == "/api/open_location":
            self._handle_open_location(data)
        elif self.path == "/api/cancel_scan":
            self._send_json({"cancelled": _signal_cancel()})
        elif self.path == "/api/shutdown":
            self._send_json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self.send_error(404)

    # ---- handlers ----
    def _parse_scan_request(
        self, data: dict,
    ) -> tuple[list[Path], list[str], int, list[Path], bool] | None:
        paths_raw = data.get("paths") or []
        if not paths_raw:
            self._send_err("no folders provided")
            return None
        paths = [Path(p) for p in paths_raw]
        for p in paths:
            if not p.is_dir():
                self._send_err(f"not a directory: {p}")
                return None
        try:
            min_size = max(0, int(data.get("min_size") or 1))
        except (TypeError, ValueError):
            min_size = 1
        skip_raw = data.get("skip") or []
        if isinstance(skip_raw, str):
            skip_raw = [s.strip() for s in skip_raw.split(",") if s.strip()]
        skip = list(skip_raw) + df.DEFAULT_SKIP
        protected_roots = _coerce_path_list(data.get("protected_paths"))
        cache_enabled = bool(data.get("hash_cache", True))
        return paths, skip, min_size, protected_roots, cache_enabled

    def _handle_scan(self, data: dict) -> None:
        parsed = self._parse_scan_request(data)
        if parsed is None:
            return
        paths, skip, min_size, protected_roots, cache_enabled = parsed
        cancel = _begin_scan()
        try:
            result = _scan_result_payload(
                paths, skip, min_size, protected_roots, cancel,
                cache_enabled=cache_enabled,
            )
        except df.ScanCancelled:
            self._send_json({"cancelled": True, "groups": [], "total_groups": 0,
                             "total_wasted": 0, "total_recoverable": 0,
                             "total_files": 0})
            return
        finally:
            _end_scan(cancel)
        self._send_json(result)

    def _handle_scan_start(self, data: dict) -> None:
        parsed = self._parse_scan_request(data)
        if parsed is None:
            return
        paths, skip, min_size, protected_roots, cache_enabled = parsed
        cancel = _begin_scan()
        job_id = _create_scan_job(cancel)
        _update_scan_job(job_id, cache_enabled=cache_enabled)
        worker = threading.Thread(
            target=_run_scan_job,
            args=(job_id, paths, skip, min_size, protected_roots, cancel, cache_enabled),
            daemon=True,
        )
        worker.start()
        self._send_json(_public_scan_job(job_id) or {"job_id": job_id})

    def _handle_scan_status(self, data: dict) -> None:
        job_id = data.get("job_id")
        if not job_id:
            self._send_err("no job_id")
            return
        job = _public_scan_job(str(job_id))
        if job is None:
            self._send_err("scan job not found", status=404)
            return
        self._send_json(job)

    def _handle_execute(self, data: dict) -> None:
        permanent = bool(data.get("permanent", False))
        items = data.get("deletions") or []
        if not items:
            self._send_err("nothing to delete")
            return
        candidates: list[df.DeletionCandidate] = []
        for it in items:
            try:
                mtime_raw = it.get("mtime_ts")
                candidates.append(df.DeletionCandidate(
                    path=Path(it["path"]),
                    size=int(it["size"]),
                    mtime_ts=float(mtime_raw) if mtime_raw is not None else None,
                    sha256=it.get("sha256") or None,
                ))
            except (KeyError, TypeError, ValueError):
                self._send_err("bad deletion entry")
                return
        protected_roots = _coerce_path_list(data.get("protected_paths"))
        deletions, validation_errors = df.validate_deletion_candidates(
            candidates, protected_roots=protected_roots,
        )

        if permanent:
            count, freed, errors = df.permanent_delete_files(deletions)
            self._send_json({
                "mode": "permanent",
                "count": count,
                "freed": freed,
                "errors": validation_errors + errors,
            })
        else:
            ts = time.strftime("%Y%m%d-%H%M%S")
            qroot = df.app_data_dir() / df.QUARANTINE_DIRNAME / f"run-{ts}"
            count, freed, manifest, errors = df.quarantine_files(deletions, qroot)
            self._send_json({
                "mode": "quarantine",
                "count": count,
                "freed": freed,
                "manifest_path": str(qroot / "manifest.json") if manifest else None,
                "quarantine_dir": str(qroot),
                "errors": validation_errors + errors,
            })

    def _handle_scan_empty(self, data: dict) -> None:
        paths_raw = data.get("paths") or []
        if not paths_raw:
            self._send_err("no folders provided")
            return
        paths = [Path(p) for p in paths_raw]
        for p in paths:
            if not p.is_dir():
                self._send_err(f"not a directory: {p}")
                return
        skip_raw = data.get("skip") or []
        if isinstance(skip_raw, str):
            skip_raw = [s.strip() for s in skip_raw.split(",") if s.strip()]
        skip = list(skip_raw) + df.DEFAULT_SKIP
        ignore_junk = bool(data.get("ignore_junk", True))

        cancel = _begin_scan()
        try:
            empties = df.find_empty_folders(paths, skip,
                                            ignore_junk=ignore_junk, cancel=cancel)
        except df.ScanCancelled:
            self._send_json({"cancelled": True, "folders": [], "count": 0})
            return
        finally:
            _end_scan(cancel)
        self._send_json({
            "folders": [str(p) for p in empties],
            "count": len(empties),
        })

    def _handle_scan_largest(self, data: dict) -> None:
        paths_raw = data.get("paths") or []
        if not paths_raw:
            self._send_err("no folders provided")
            return
        paths = [Path(p) for p in paths_raw]
        for p in paths:
            if not p.is_dir():
                self._send_err(f"not a directory: {p}")
                return
        try:
            min_size = max(0, int(data.get("min_size") or 1))
        except (TypeError, ValueError):
            min_size = 1
        try:
            top_files = max(1, min(int(data.get("top_files") or 200), 2000))
        except (TypeError, ValueError):
            top_files = 200
        try:
            top_folders = max(1, min(int(data.get("top_folders") or 50), 500))
        except (TypeError, ValueError):
            top_folders = 50
        skip_raw = data.get("skip") or []
        if isinstance(skip_raw, str):
            skip_raw = [s.strip() for s in skip_raw.split(",") if s.strip()]
        skip = list(skip_raw) + df.DEFAULT_SKIP

        cancel = _begin_scan()
        try:
            files, folders, total_files, total_bytes = df.compute_top_largest(
                paths, skip,
                min_size=min_size,
                top_files=top_files,
                top_folders=top_folders,
                cancel=cancel,
            )
        except df.ScanCancelled:
            self._send_json({"cancelled": True, "files": [], "folders": [],
                             "total_files": 0, "total_bytes": 0})
            return
        finally:
            _end_scan(cancel)

        # Use the user-picked roots as anchors for short paths.
        anchor = df.common_ancestor(paths) if len(paths) > 1 else paths[0]
        self._send_json({
            "files": [{
                "path": str(f.path),
                "short": df.short_paths([f.path], anchor)[0],
                "size": f.size,
                "size_label": df.format_size(f.size),
                "mtime_ts": f.mtime_ts,
                "mtime": df.format_mtime(f.path),
            } for f in files],
            "folders": [{
                "path": str(f.path),
                "short": df.short_paths([f.path], anchor)[0],
                "total_size": f.total_size,
                "total_size_label": df.format_size(f.total_size),
                "direct_size": f.direct_size,
                "file_count": f.file_count,
            } for f in folders],
            "total_files": total_files,
            "total_bytes": total_bytes,
            "total_bytes_label": df.format_size(total_bytes),
        })

    def _handle_scan_similar_start(self, data: dict) -> None:
        if not df.pillow_available():
            self._send_json({
                "pillow_available": False,
                "error": "Pillow is not installed. Run: pip install Pillow",
            })
            return
        paths_raw = data.get("paths") or []
        if not paths_raw:
            self._send_err("no folders provided")
            return
        paths = [Path(p) for p in paths_raw]
        for p in paths:
            if not p.is_dir():
                self._send_err(f"not a directory: {p}")
                return
        try:
            min_size = max(0, int(data.get("min_size") or 1))
        except (TypeError, ValueError):
            min_size = 1
        try:
            threshold = max(0, min(int(data.get("threshold") or
                                       df.DEFAULT_SIMILARITY_THRESHOLD), 30))
        except (TypeError, ValueError):
            threshold = df.DEFAULT_SIMILARITY_THRESHOLD
        skip_raw = data.get("skip") or []
        if isinstance(skip_raw, str):
            skip_raw = [s.strip() for s in skip_raw.split(",") if s.strip()]
        skip = list(skip_raw) + df.DEFAULT_SKIP

        cancel = _begin_scan()
        job_id = _create_scan_job(cancel)
        worker = threading.Thread(
            target=_run_similar_images_job,
            args=(job_id, paths, skip, min_size, threshold, cancel),
            daemon=True,
        )
        worker.start()
        payload = _public_scan_job(job_id) or {"job_id": job_id}
        payload["pillow_available"] = True
        self._send_json(payload)

    def _handle_scan_similar_images(self, data: dict) -> None:
        if not df.pillow_available():
            self._send_json({
                "pillow_available": False,
                "error": "Pillow is not installed. Run: pip install Pillow",
                "groups": [], "scanned": 0, "skipped": 0,
            }, status=200)
            return
        paths_raw = data.get("paths") or []
        if not paths_raw:
            self._send_err("no folders provided")
            return
        paths = [Path(p) for p in paths_raw]
        for p in paths:
            if not p.is_dir():
                self._send_err(f"not a directory: {p}")
                return
        try:
            min_size = max(0, int(data.get("min_size") or 1))
        except (TypeError, ValueError):
            min_size = 1
        try:
            threshold = max(0, min(int(data.get("threshold") or
                                       df.DEFAULT_SIMILARITY_THRESHOLD), 30))
        except (TypeError, ValueError):
            threshold = df.DEFAULT_SIMILARITY_THRESHOLD
        skip_raw = data.get("skip") or []
        if isinstance(skip_raw, str):
            skip_raw = [s.strip() for s in skip_raw.split(",") if s.strip()]
        skip = list(skip_raw) + df.DEFAULT_SKIP

        cancel = _begin_scan()
        try:
            groups, scanned, skipped = df.find_similar_images(
                paths, skip, threshold=threshold, min_size=min_size, cancel=cancel,
            )
        except df.ScanCancelled:
            self._send_json({"cancelled": True, "pillow_available": True,
                             "groups": [], "scanned": 0, "skipped": 0})
            return
        finally:
            _end_scan(cancel)

        anchor = df.common_ancestor(paths) if len(paths) > 1 else paths[0]
        groups_out = []
        for g in groups:
            # Default keeper heuristic: largest file (already sorted by size desc),
            # i.e., keep_indices = [0].
            files = []
            for im in g:
                files.append({
                    "path": str(im.path),
                    "short": df.short_paths([im.path], anchor)[0],
                    "size": im.size,
                    "size_label": df.format_size(im.size),
                    "mtime": df.format_mtime(im.path),
                    "mtime_ts": im.mtime_ts,
                    "width": im.width,
                    "height": im.height,
                    "resolution": f"{im.width}x{im.height}" if im.width else "?",
                    "thumbnail": im.thumbnail,
                })
            wasted = sum(im.size for im in g[1:])
            groups_out.append({
                "files": files,
                "count": len(files),
                "keep_indices": [0],     # default: keep first (largest)
                "wasted": wasted,
                "wasted_label": df.format_size(wasted),
            })

        self._send_json({
            "pillow_available": True,
            "groups": groups_out,
            "total_groups": len(groups_out),
            "scanned": scanned,
            "skipped": skipped,
            "threshold": threshold,
            "total_wasted": sum(g["wasted"] for g in groups_out),
        })

    def _handle_delete_empty(self, data: dict) -> None:
        items = data.get("folders") or []
        if not items:
            self._send_err("nothing to delete")
            return
        folders = [Path(p) for p in items]
        count, errors = df.delete_empty_folders(folders)
        self._send_json({"count": count, "errors": errors})

    def _handle_restore(self, data: dict) -> None:
        manifest = data.get("manifest_path")
        if not manifest:
            self._send_err("no manifest")
            return
        rc = df.restore_from_manifest(Path(manifest))
        self._send_json({"return_code": rc})

    def _handle_quarantine_history(self) -> None:
        qbase = df.app_data_dir() / df.QUARANTINE_DIRNAME
        self._send_json({"runs": df.list_quarantine_runs(qbase)})

    def _handle_preview(self, data: dict) -> None:
        path_str = data.get("path")
        if not path_str:
            self._send_err("no path")
            return
        payload = df.preview_file(Path(path_str))
        status = 200 if payload.get("kind") != "error" else 400
        self._send_json(payload, status=status)

    def _handle_open_location(self, data: dict) -> None:
        path_str = data.get("path")
        if not path_str:
            self._send_err("no path")
            return
        p = Path(path_str)
        if not p.exists():
            self._send_err(f"path does not exist: {p}", status=404)
            return
        try:
            if sys.platform == "win32":
                if p.is_dir():
                    os.startfile(str(p))  # type: ignore[attr-defined]
                else:
                    # Open Explorer with the file highlighted.
                    subprocess.Popen(["explorer", f"/select,{p}"])
            elif sys.platform == "darwin":
                args = ["open", "-R", str(p)] if p.is_file() else ["open", str(p)]
                subprocess.Popen(args)
            else:
                target = p if p.is_dir() else p.parent
                subprocess.Popen(["xdg-open", str(target)])
            self._send_json({"ok": True})
        except Exception as e:  # noqa: BLE001
            self._send_err(f"failed to open: {e}", status=500)


# =============================================================================
# Server boot
# =============================================================================

class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _find_free_port(start: int) -> int:
    p = start
    while p < start + 50:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, p))
                return p
            except OSError:
                p += 1
    return start


def _find_chromium_exe() -> str | None:
    """Locate Edge or Chrome on Windows."""
    if os.name != "nt":
        return None
    for exe in ("msedge.exe", "chrome.exe"):
        full = shutil.which(exe)
        if full:
            return full
    for guess in (
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Google/Chrome/Application/chrome.exe",
    ):
        if guess.is_file():
            return str(guess)
    return None


def _launch_browser_tracked(url: str) -> subprocess.Popen | None:
    """Launch Edge/Chrome in --app mode under a dedicated user-data-dir.

    Returns the Popen so the caller can wait on it. When the user closes
    the window, that process exits and the caller can shut the server down.

    A dedicated --user-data-dir is essential: without it, --app=URL on a
    system that already has Edge open would just send a "open new window"
    message to the existing Edge process, our Popen would return
    immediately, and we'd think the window had already closed.
    """
    exe = _find_chromium_exe()
    if not exe:
        return None
    import tempfile
    user_data_dir = Path(tempfile.gettempdir()) / f"sift_browser_profile_{os.getpid()}"
    try:
        user_data_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    try:
        return subprocess.Popen([
            exe,
            f"--app={url}",
            f"--user-data-dir={user_data_dir}",
            "--no-default-browser-check",
            "--no-first-run",
            "--disable-features=TranslateUI",
        ])
    except Exception:  # noqa: BLE001
        return None


def _launch_browser_detached(url: str) -> None:
    """Fallback: open the system default browser (we can't track lifecycle)."""
    webbrowser.open(url)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="sift",
                                     description="Sift — local web UI server.")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't auto-open a browser window. The server still "
                             "runs; open the printed URL manually when ready.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Preferred port (default: {DEFAULT_PORT}). "
                             "Falls back to the next free port if busy.")
    args = parser.parse_args()

    if not UI_FILE.is_file():
        print(f"error: UI file missing: {UI_FILE}", file=sys.stderr)
        return 2

    _set_process_dpi_aware()
    port = _find_free_port(args.port)
    server = ThreadedServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}/"
    print(f"Sift running at {url}")

    if args.no_browser:
        print("(no-browser mode: open the URL above in any browser, "
              "or double-click open_ui.vbs)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            print("Stopped.")
        return 0

    # GUI mode: server runs in a background thread; main thread tracks
    # the browser process so that closing the window cleanly stops the app.
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    browser_proc = _launch_browser_tracked(url)
    if browser_proc is None:
        # Couldn't spawn a tracked browser — best effort: open default browser
        # and stay alive until Ctrl+C. Window-close-to-exit only works with
        # Edge/Chrome.
        print("[sift] Edge/Chrome not found; opening default browser. "
              "App will keep running — kill via Task Manager when done.")
        _launch_browser_detached(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
        return 0

    print("Sift window is open. Close it to exit.")
    try:
        browser_proc.wait()
    except KeyboardInterrupt:
        try:
            browser_proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    finally:
        server.shutdown()
        server.server_close()
        # Best-effort cleanup of the temp browser profile.
        try:
            import tempfile
            shutil.rmtree(Path(tempfile.gettempdir()) /
                          f"sift_browser_profile_{os.getpid()}",
                          ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
        print("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
