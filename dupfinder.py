"""Sift — find duplicates, similar photos, empty folders, and large files safely.

Sift sifts through your folders and surfaces what's wasting space,
with a safety net (quarantine + restore) so you can clean up without fear.

Core scan modes:
    (default)           find byte-identical duplicate files
    --find-empty        find recursively-empty folders
    --largest           find largest files and folders
    --similar-images    find visually similar photos (Pillow required)

Duplicate-file removal modes:
    --delete            interactive per-group menu (default action: quarantine)
    --auto POLICY       no prompts: keep-newest | keep-oldest | keep-shortest
                                  | keep-in <PATH>
    --permanent         actually unlink instead of moving to quarantine
    --restore PATH      restore files listed in a previous run's manifest

The Python module is still named `dupfinder` for backwards compatibility
with existing imports; user-facing strings say "Sift".
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import heapq
import json
import mimetypes
import os
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Literal, Optional


PARTIAL_BYTES = 8 * 1024
CHUNK = 64 * 1024
DEFAULT_SKIP = [".git", "node_modules", "__pycache__",
                ".dupfinder_quarantine", ".dupfinder_cache.sqlite3",
                ".dupfinder_cache.sqlite3-shm", ".dupfinder_cache.sqlite3-wal"]
QUARANTINE_DIRNAME = ".dupfinder_quarantine"
HASH_CACHE_FILENAME = ".dupfinder_cache.sqlite3"
JUNK_FILES = {"thumbs.db", ".ds_store", "desktop.ini"}
PREVIEW_TEXT_BYTES = 32 * 1024
PREVIEW_IMAGE_BYTES = 8 * 1024 * 1024
RASTER_IMAGE_MIMES = {
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
}
SIMILAR_IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff", ".tif",
}
DEFAULT_SIMILARITY_THRESHOLD = 5
THUMBNAIL_SIZE = 160
TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".css", ".js",
    ".ts", ".py", ".log", ".ini", ".cfg", ".toml", ".yaml", ".yml",
}


class ScanCancelled(Exception):
    """Raised when a scan is interrupted via the cancel signal."""


# =============================================================================
# Data directory (where quarantine + hash cache live)
# =============================================================================

_cached_data_dir: Path | None = None


def _resolve_data_dir(candidates: list[Path]) -> Path:
    """Return the first candidate we can actually write into.

    For each candidate: try to mkdir(parents=True, exist_ok=True), then
    write+delete a probe file to confirm we have write permission. Raises
    OSError if none of the candidates work.
    """
    last_err: OSError | None = None
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / f".dupfinder_probe_{os.getpid()}"
            probe.touch()
            probe.unlink()
            return cand
        except OSError as e:
            last_err = e
            continue
    raise last_err or OSError("no writable data dir candidate")


def app_data_dir() -> Path:
    """Where dupfinder writes its state (quarantine, hash cache).

    Strategy:
      1. Path.cwd() — preserves the existing behavior for users who launch
         from a project folder via .vbs or a normal shell.
      2. ~/.dupfinder — used when cwd is not writable (e.g., the .exe is
         double-clicked from C:\\ root or from C:\\Windows\\System32 without
         admin rights).
      3. <temp>/dupfinder — last-resort fallback so the app never crashes
         just because state can't be written.

    Cached after the first call.
    """
    global _cached_data_dir
    if _cached_data_dir is not None:
        return _cached_data_dir

    import tempfile
    cwd = Path.cwd()
    candidates = [
        cwd,
        Path.home() / ".dupfinder",
        Path(tempfile.gettempdir()) / "dupfinder",
    ]
    try:
        resolved = _resolve_data_dir(candidates)
    except OSError:
        # All three failed: return cwd so the caller surfaces the real error.
        resolved = cwd

    _cached_data_dir = resolved
    if resolved != cwd:
        print(f"[dupfinder] current directory not writable; "
              f"using {resolved} for state files",
              file=sys.stderr)
    return resolved


def reset_app_data_dir_cache() -> None:
    """For tests: forget the cached data dir so the next call recomputes."""
    global _cached_data_dir
    _cached_data_dir = None


def _check_cancel(cancel) -> None:
    """Raise ScanCancelled if the optional cancel signal is set.

    `cancel` is anything with an .is_set() method (e.g., threading.Event) or None.
    """
    if cancel is not None and cancel.is_set():
        raise ScanCancelled()


# =============================================================================
# Scanning
# =============================================================================

def iter_files(
    roots: Iterable[Path],
    skip_patterns: list[str],
    min_size: int,
    cancel=None,
) -> Iterator[tuple[Path, int, tuple[int, int]]]:
    seen_count = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_error):
            _check_cancel(cancel)
            dirnames[:] = [d for d in dirnames if not _matches(d, skip_patterns)]
            for name in filenames:
                _check_cancel(cancel)
                if _matches(name, skip_patterns):
                    continue
                p = Path(dirpath) / name
                try:
                    st = p.stat()
                except OSError as e:
                    print(f"skip (stat failed): {p} — {e}", file=sys.stderr)
                    continue
                if not os.path.isfile(p) or st.st_size < min_size:
                    continue
                seen_count += 1
                if seen_count % 2000 == 0:
                    print(f"  scanned {seen_count} files...", file=sys.stderr)
                yield p, st.st_size, (st.st_dev, st.st_ino)


def _walk_error(err: OSError) -> None:
    print(f"skip (walk error): {err}", file=sys.stderr)


def _matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def group_by_size(
    entries: Iterable[tuple[Path, int, tuple[int, int]]],
) -> dict[int, list[tuple[Path, tuple[int, int]]]]:
    groups: dict[int, list[tuple[Path, tuple[int, int]]]] = defaultdict(list)
    for path, size, ident in entries:
        groups[size].append((path, ident))
    return {size: items for size, items in groups.items() if len(items) > 1}


class HashCache:
    """Tiny SQLite cache keyed by path + size + mtime + hash mode."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.conn: sqlite3.Connection | None = None
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self.errors = 0

    def __enter__(self) -> "HashCache":
        self._ensure()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _ensure(self) -> sqlite3.Connection:
        if self.conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.db_path)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS file_hashes (
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    partial INTEGER NOT NULL,
                    algorithm TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    PRIMARY KEY (path, size, mtime_ns, partial, algorithm)
                )
            """)
            self.conn.commit()
        return self.conn

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def get(self, path: Path, size: int, mtime_ns: int, partial: bool) -> str | None:
        try:
            row = self._ensure().execute(
                "SELECT digest FROM file_hashes "
                "WHERE path = ? AND size = ? AND mtime_ns = ? "
                "AND partial = ? AND algorithm = ?",
                (str(path), size, mtime_ns, int(partial), "sha256"),
            ).fetchone()
        except sqlite3.Error:
            self.errors += 1
            return None
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        return str(row[0])

    def set(
        self,
        path: Path,
        size: int,
        mtime_ns: int,
        partial: bool,
        digest: str,
    ) -> None:
        try:
            self._ensure().execute(
                "INSERT OR REPLACE INTO file_hashes "
                "(path, size, mtime_ns, partial, algorithm, digest, updated_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(path), size, mtime_ns, int(partial), "sha256", digest,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            self._ensure().commit()
            self.writes += 1
        except sqlite3.Error:
            self.errors += 1

    def stats(self) -> dict[str, int]:
        return {
            "cache_hits": self.hits,
            "cache_misses": self.misses,
            "cache_writes": self.writes,
            "cache_errors": self.errors,
        }


def _hash_cache_key(path: Path) -> tuple[Path, int, int] | None:
    try:
        st = path.stat()
        return path.resolve(), st.st_size, st.st_mtime_ns
    except OSError:
        return None


def hash_file(
    path: Path,
    partial: bool,
    cache: HashCache | None = None,
    cancel=None,
) -> str | None:
    cache_key = _hash_cache_key(path) if cache is not None else None
    if cache is not None and cache_key is not None:
        cached = cache.get(*cache_key, partial=partial)
        if cached is not None:
            return cached

    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            if partial:
                h.update(f.read(PARTIAL_BYTES))
            else:
                while True:
                    _check_cancel(cancel)
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
    except OSError as e:
        print(f"skip (read failed): {path} — {e}", file=sys.stderr)
        return None
    digest = h.hexdigest()
    if cache is not None and cache_key is not None:
        cache.set(*cache_key, partial=partial, digest=digest)
    return digest


def _refine_by_hash(
    items: list[tuple[Path, tuple[int, int]]],
    partial: bool,
    cancel=None,
    progress=None,
    cache: HashCache | None = None,
) -> list[list[tuple[Path, tuple[int, int]]]]:
    buckets: dict[str, list[tuple[Path, tuple[int, int]]]] = defaultdict(list)
    for path, ident in items:
        _check_cancel(cancel)
        before_hits = cache.hits if cache is not None else 0
        digest = hash_file(path, partial=partial, cache=cache, cancel=cancel)
        cached = cache is not None and cache.hits > before_hits
        if progress is not None:
            progress(path, partial, digest is not None, cached)
        if digest is None:
            continue
        buckets[digest].append((path, ident))
    return [g for g in buckets.values() if len(g) > 1]


def _dedupe_hardlinks(group: list[tuple[Path, tuple[int, int]]]) -> list[Path]:
    seen: set[tuple[int, int]] = set()
    out: list[Path] = []
    for path, ident in group:
        key = ident if ident[1] != 0 else (ident[0], id(path))
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def find_duplicates(
    size_groups: dict[int, list[tuple[Path, tuple[int, int]]]],
    cancel=None,
    progress=None,
    cache: HashCache | None = None,
) -> list[tuple[int, list[Path]]]:
    results: list[tuple[int, list[Path]]] = []
    for size, items in size_groups.items():
        _check_cancel(cancel)
        for pg in _refine_by_hash(
            items, partial=True, cancel=cancel, progress=progress, cache=cache,
        ):
            for fg in _refine_by_hash(
                pg, partial=False, cancel=cancel, progress=progress, cache=cache,
            ):
                paths = _dedupe_hardlinks(fg)
                if len(paths) > 1:
                    paths.sort()
                    results.append((size, paths))
    results.sort(key=lambda r: r[0] * (len(r[1]) - 1), reverse=True)
    return results


# =============================================================================
# Empty folder finder
# =============================================================================

def find_empty_folders(
    roots: Iterable[Path],
    skip_patterns: list[str],
    ignore_junk: bool = True,
    cancel=None,
) -> list[Path]:
    """Find folders that are recursively empty (no files anywhere inside).

    With ignore_junk=True, OS metadata files (Thumbs.db, .DS_Store, desktop.ini)
    don't count — a folder containing only those is treated as empty.

    The user-picked roots themselves are never reported, even if empty.
    Result is sorted deepest-first so callers can rmdir in order.
    """
    roots_resolved = {Path(r).resolve() for r in roots}
    empty_set: set[Path] = set()

    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, topdown=False, onerror=_walk_error):
            _check_cancel(cancel)
            d = Path(dirpath)
            d_resolved = d.resolve()

            if d_resolved in roots_resolved:
                continue
            if _matches(d.name, skip_patterns):
                continue

            real_files = filenames
            if ignore_junk:
                real_files = [f for f in filenames if f.lower() not in JUNK_FILES]
            if real_files:
                continue

            # All non-skipped subdirs must themselves be reported empty.
            all_subs_empty = True
            for sub in dirnames:
                if _matches(sub, skip_patterns):
                    continue
                if (d / sub).resolve() not in empty_set:
                    all_subs_empty = False
                    break
            if not all_subs_empty:
                continue

            empty_set.add(d_resolved)

    return sorted(empty_set, key=lambda p: (-len(p.parts), str(p)))


def delete_empty_folders(folders: list[Path]) -> tuple[int, list[str]]:
    """Delete folders identified as empty. Uses rmdir, which only succeeds if
    actually empty (junk files are removed first). Returns (count, errors).

    Sorts deepest-first internally so nested empty trees collapse correctly.
    """
    errors: list[str] = []
    count = 0
    ordered = sorted(folders, key=lambda p: (-len(p.parts), str(p)))
    for f in ordered:
        try:
            for entry in f.iterdir():
                if entry.is_file() and entry.name.lower() in JUNK_FILES:
                    try:
                        entry.unlink()
                    except OSError as e:
                        errors.append(f"{entry} (junk cleanup) — {e}")
            f.rmdir()
            count += 1
        except OSError as e:
            errors.append(f"{f} — {e}")
    return count, errors


# =============================================================================
# Similar images (perceptual hashing for photo culling)
# =============================================================================

def pillow_available() -> bool:
    """True when Pillow is importable. Cached after first call."""
    if not hasattr(pillow_available, "_cached"):
        try:
            import PIL  # noqa: F401
            pillow_available._cached = True
        except ImportError:
            pillow_available._cached = False
    return pillow_available._cached


def compute_image_dhash(path: Path, hash_size: int = 8) -> int | None:
    """64-bit perceptual hash via difference hashing.

    Robust to resize, recompression, light blur, color changes. NOT robust to
    rotation, severe crop, or heavy color inversion. Two images with Hamming
    distance ≤ 5 are usually visually the same shot.
    """
    if not pillow_available():
        return None
    from PIL import Image, ImageOps
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)            # honor camera EXIF rotation
            img = img.convert("L").resize(
                (hash_size + 1, hash_size), Image.Resampling.LANCZOS,
            )
            pixels = list(img.getdata())
    except Exception:  # noqa: BLE001  — Pillow raises many decode-related errors
        return None
    bits = 0
    stride = hash_size + 1
    for row in range(hash_size):
        base = row * stride
        for col in range(hash_size):
            bits = (bits << 1) | (1 if pixels[base + col] > pixels[base + col + 1] else 0)
    return bits


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def compute_image_thumbnail(path: Path, max_side: int = THUMBNAIL_SIZE) -> str | None:
    """Return a small JPEG data URL for inline display, or None on failure."""
    if not pillow_available():
        return None
    from PIL import Image, ImageOps
    from io import BytesIO
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=72, optimize=True)
            return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"
    except Exception:  # noqa: BLE001
        return None


@dataclass
class SimilarImage:
    path: Path
    size: int
    mtime_ts: float
    width: int
    height: int
    dhash: int
    thumbnail: str | None


# EXIF orientation tag that swaps width/height when set to 5..8.
_EXIF_ORIENTATION_TAG = 274


def _compute_image_signature(
    path: Path, hash_size: int = 8, thumb_size: int = THUMBNAIL_SIZE,
) -> tuple[int, int, int, str | None] | None:
    """One-open-per-image: return (dhash, width, height, thumbnail_data_url).

    JPEGs use Pillow's draft() mode to ask libjpeg to decode at reduced
    resolution (typically 1/4 or 1/8 of original), giving ~10x speedup
    on large DSLR files. dHash is stable across draft levels because the
    final resize to hash_size+1 × hash_size collapses any residual scale
    differences.
    """
    if not pillow_available():
        return None
    from PIL import Image, ImageOps
    from io import BytesIO
    try:
        with Image.open(path) as img:
            # Original (pre-EXIF-rotation) header size.
            try:
                exif = img.getexif()
                orientation = exif.get(_EXIF_ORIENTATION_TAG, 1) if exif else 1
            except Exception:  # noqa: BLE001
                orientation = 1
            ow, oh = img.size
            if orientation in (5, 6, 7, 8):
                ow, oh = oh, ow

            # Ask the decoder for a smaller decoded image. For JPEG this is
            # the big win — libjpeg downsamples in the DCT domain. For other
            # formats draft() is a no-op or unsupported (caught below).
            try:
                img.draft("RGB", (thumb_size, thumb_size))
            except (AttributeError, OSError):
                pass

            img = ImageOps.exif_transpose(img)
            # Thumbnail (color)
            thumb = img.copy()
            thumb.thumbnail((thumb_size, thumb_size), Image.Resampling.LANCZOS)
            if thumb.mode != "RGB":
                thumb = thumb.convert("RGB")
            buf = BytesIO()
            thumb.save(buf, format="JPEG", quality=72, optimize=True)
            thumbnail = (
                f"data:image/jpeg;base64,"
                f"{base64.b64encode(buf.getvalue()).decode('ascii')}"
            )

            # dHash (grayscale, hash_size+1 × hash_size)
            tiny = img.convert("L").resize(
                (hash_size + 1, hash_size), Image.Resampling.LANCZOS,
            )
            pixels = list(tiny.getdata())
    except Exception:  # noqa: BLE001 — any decoder failure = skip
        return None

    bits = 0
    stride = hash_size + 1
    for row in range(hash_size):
        base = row * stride
        for col in range(hash_size):
            bits = (bits << 1) | (1 if pixels[base + col] > pixels[base + col + 1] else 0)
    return bits, ow, oh, thumbnail


def _image_resolution(path: Path) -> tuple[int, int]:
    if not pillow_available():
        return (0, 0)
    from PIL import Image
    try:
        with Image.open(path) as img:
            return img.size  # (width, height)
    except Exception:  # noqa: BLE001
        return (0, 0)


def find_similar_images(
    roots: Iterable[Path],
    skip_patterns: list[str],
    threshold: int = DEFAULT_SIMILARITY_THRESHOLD,
    min_size: int = 1,
    cancel=None,
    progress=None,  # called as progress(stage, current, total)
) -> tuple[list[list[SimilarImage]], int, int]:
    """Find groups of visually similar images.

    Returns (groups, scanned_count, skipped_count). Each group has ≥ 2 images
    whose dHashes form a transitively-connected component within `threshold`
    Hamming distance.

    Requires Pillow. Returns ([], 0, 0) if Pillow is unavailable; caller
    should check `pillow_available()` first to surface a useful error.
    """
    if not pillow_available():
        return [], 0, 0

    # Stage 1: gather candidate image paths.
    candidates: list[tuple[Path, int, float]] = []
    for p, size, _ident in iter_files(roots, skip_patterns, min_size, cancel=cancel):
        if p.suffix.lower() in SIMILAR_IMAGE_EXTS:
            try:
                mt = p.stat().st_mtime
            except OSError:
                mt = 0.0
            candidates.append((p, size, mt))

    total = len(candidates)
    if total < 2:
        return [], total, 0

    if progress is not None:
        progress("hashing", 0, total)

    # Stage 2: compute (dHash, dimensions, thumbnail) for each image in one
    # decode per file, parallelized across CPU cores (Pillow releases the GIL
    # during image I/O so this scales near-linearly with worker count).
    from concurrent.futures import ThreadPoolExecutor, as_completed
    max_workers = min(8, (os.cpu_count() or 4))

    images: list[SimilarImage] = []
    skipped = 0
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as exe:
        futures = {
            exe.submit(_compute_image_signature, p): (p, size, mt)
            for p, size, mt in candidates
        }
        try:
            for fut in as_completed(futures):
                _check_cancel(cancel)
                p, size, mt = futures[fut]
                try:
                    sig = fut.result()
                except Exception:  # noqa: BLE001
                    sig = None
                if sig is None:
                    skipped += 1
                else:
                    h, w, ht, thumb = sig
                    images.append(SimilarImage(
                        path=p, size=size, mtime_ts=mt,
                        width=w, height=ht, dhash=h, thumbnail=thumb,
                    ))
                completed += 1
                if progress is not None and completed % 25 == 0:
                    progress("hashing", completed, total)
        except ScanCancelled:
            # Best-effort: cancel any not-yet-started futures. Running ones
            # finish their current image (Pillow C code can't be interrupted).
            for f in futures:
                f.cancel()
            raise
    if progress is not None:
        progress("hashing", total, total)

    n = len(images)
    if n < 2:
        return [], total, skipped

    # Stage 3: pairwise Hamming + union-find clustering.
    if progress is not None:
        progress("clustering", 0, n)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        _check_cancel(cancel)
        for j in range(i + 1, n):
            if hamming_distance(images[i].dhash, images[j].dhash) <= threshold:
                union(i, j)
        if progress is not None and i % 50 == 0:
            progress("clustering", i, n)

    groups_dict: dict[int, list[SimilarImage]] = defaultdict(list)
    for i, img in enumerate(images):
        groups_dict[find(i)].append(img)
    groups = [sorted(g, key=lambda im: (-im.size, str(im.path)))
              for g in groups_dict.values() if len(g) > 1]
    # Largest group / largest waste first.
    groups.sort(key=lambda g: sum(im.size for im in g[1:]), reverse=True)
    return groups, total, skipped


# =============================================================================
# Largest items (files + folders by total size)
# =============================================================================

@dataclass
class LargestFileEntry:
    path: Path
    size: int
    mtime_ts: float


@dataclass
class LargestFolderEntry:
    path: Path
    total_size: int       # recursive sum of all files under this folder
    direct_size: int      # bytes from files directly in this folder
    file_count: int       # recursive count of files


def compute_top_largest(
    roots: Iterable[Path],
    skip_patterns: list[str],
    min_size: int = 1,
    top_files: int = 200,
    top_folders: int = 50,
    cancel=None,
) -> tuple[list[LargestFileEntry], list[LargestFolderEntry], int, int]:
    """Walk roots and return (top_files, top_folders, total_files, total_bytes).

    Single pass:
      - Each file: stat once, contribute to its parent dir's direct_size,
        try to enter the min-heap of top-N largest files.
      - Each dir visited (even those with only subdirs) is tracked.
    Then folds child sizes into parents by walking the dir set from deepest
    to shallowest.
    """
    file_heap: list[tuple[int, str, float]] = []  # min-heap of (size, path_str, mtime)
    direct_size: dict[Path, int] = defaultdict(int)
    direct_count: dict[Path, int] = defaultdict(int)
    all_dirs: set[Path] = set()
    total_files = 0
    total_bytes = 0

    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_error):
            _check_cancel(cancel)
            dirnames[:] = [d for d in dirnames if not _matches(d, skip_patterns)]
            d = Path(dirpath)
            all_dirs.add(d)
            for name in filenames:
                _check_cancel(cancel)
                if _matches(name, skip_patterns):
                    continue
                p = d / name
                try:
                    st = p.stat()
                except OSError as e:
                    print(f"skip (stat failed): {p} — {e}", file=sys.stderr)
                    continue
                if not os.path.isfile(p) or st.st_size < min_size:
                    continue
                size = st.st_size
                direct_size[d] += size
                direct_count[d] += 1
                total_files += 1
                total_bytes += size
                entry = (size, str(p), st.st_mtime)
                if len(file_heap) < top_files:
                    heapq.heappush(file_heap, entry)
                elif size > file_heap[0][0]:
                    heapq.heapreplace(file_heap, entry)

    # Fold child folder sizes into parents (deepest first).
    folder_total: dict[Path, int] = {d: direct_size.get(d, 0) for d in all_dirs}
    folder_count_total: dict[Path, int] = {d: direct_count.get(d, 0) for d in all_dirs}
    for d in sorted(all_dirs, key=lambda p: len(p.parts), reverse=True):
        parent = d.parent
        if parent in folder_total and parent != d:
            folder_total[parent] += folder_total[d]
            folder_count_total[parent] += folder_count_total[d]

    # Top-N folders by recursive size.
    top_folders_items = heapq.nlargest(
        top_folders,
        ((d, folder_total[d]) for d in all_dirs if folder_total[d] > 0),
        key=lambda kv: kv[1],
    )
    folders_out = [
        LargestFolderEntry(
            path=d,
            total_size=total,
            direct_size=direct_size.get(d, 0),
            file_count=folder_count_total[d],
        )
        for d, total in top_folders_items
    ]

    # Top-N files (sort heap content largest first).
    file_heap.sort(key=lambda t: t[0], reverse=True)
    files_out = [
        LargestFileEntry(path=Path(p), size=size, mtime_ts=mtime)
        for size, p, mtime in file_heap
    ]

    return files_out, folders_out, total_files, total_bytes


# =============================================================================
# Display helpers
# =============================================================================

def format_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.2f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f} B"


def format_mtime(p: Path) -> str:
    try:
        ts = p.stat().st_mtime
    except OSError:
        return "??"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def common_ancestor(paths: list[Path]) -> Path | None:
    if not paths:
        return None
    try:
        return Path(os.path.commonpath([str(p) for p in paths]))
    except ValueError:
        # Different drives on Windows.
        return None


def short_paths(paths: list[Path], anchor: Path | None) -> list[str]:
    if anchor is None:
        return [str(p) for p in paths]
    out = []
    for p in paths:
        try:
            out.append(str(p.relative_to(anchor)))
        except ValueError:
            out.append(str(p))
    return out


# =============================================================================
# Preview helpers
# =============================================================================

def preview_file(
    path: Path,
    max_text_bytes: int = PREVIEW_TEXT_BYTES,
    max_image_bytes: int = PREVIEW_IMAGE_BYTES,
) -> dict:
    """Return a safe, bounded preview payload for the local web UI."""
    if not path.is_file():
        return {"kind": "error", "error": f"not a file: {path}", "path": str(path)}
    try:
        st = path.stat()
    except OSError as e:
        return {"kind": "error", "error": str(e), "path": str(path)}

    mime, _encoding = mimetypes.guess_type(str(path))
    mime = mime or "application/octet-stream"
    base = {
        "path": str(path),
        "name": path.name,
        "size": st.st_size,
        "size_label": format_size(st.st_size),
        "mtime": format_mtime(path),
        "mime": mime,
    }

    if mime in RASTER_IMAGE_MIMES:
        if st.st_size > max_image_bytes:
            return {
                **base,
                "kind": "metadata",
                "note": f"Image is larger than preview limit ({format_size(max_image_bytes)}).",
            }
        try:
            data = path.read_bytes()
        except OSError as e:
            return {"kind": "error", "error": str(e), **base}
        return {
            **base,
            "kind": "image",
            "data_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
        }

    looks_text = mime.startswith("text/") or path.suffix.lower() in TEXT_EXTENSIONS
    if looks_text:
        try:
            with path.open("rb") as f:
                raw = f.read(max_text_bytes + 1)
        except OSError as e:
            return {"kind": "error", "error": str(e), **base}
        if b"\x00" in raw:
            return {**base, "kind": "metadata", "note": "Binary-looking file."}
        truncated = len(raw) > max_text_bytes
        raw = raw[:max_text_bytes]
        text = raw.decode("utf-8", errors="replace")
        return {**base, "kind": "text", "text": text, "truncated": truncated}

    return {**base, "kind": "metadata", "note": "No inline preview for this file type."}


# =============================================================================
# Action parsing
# =============================================================================

@dataclass
class Action:
    kind: Literal["execute", "skip", "apply_all", "quit"]
    keep_index: int = 0
    delete_indices: list[int] = field(default_factory=list)


def parse_action(line: str, n: int) -> Optional[Action]:
    """Parse a user input line. Returns None if input is invalid."""
    s = line.strip().lower()
    if s == "" or s == "y":
        return Action("execute", keep_index=0, delete_indices=list(range(1, n)))
    if s in ("s", "skip", "n"):
        return Action("skip")
    if s in ("a", "all"):
        return Action("apply_all")
    if s in ("q", "quit", "exit"):
        return Action("quit")

    if s.startswith("k"):
        rest = s[1:].strip().lstrip(",")
        nums = _parse_indices(rest, n)
        if nums is None or len(nums) != 1:
            return None
        keep = nums[0]
        return Action("execute", keep_index=keep,
                      delete_indices=[i for i in range(n) if i != keep])

    if s.startswith("d"):
        rest = s[1:].strip().lstrip(",")
        nums = _parse_indices(rest, n)
        if nums is None or not nums:
            return None
        # Don't allow deleting all of them.
        if len(set(nums)) >= n:
            return None
        # keep_index = first not-in-delete (for reporting purposes only).
        remaining = [i for i in range(n) if i not in nums]
        return Action("execute", keep_index=remaining[0], delete_indices=sorted(set(nums)))

    return None


def _parse_indices(text: str, n: int) -> list[int] | None:
    """Parse '1,3' or '1 3' or '13' (only single digit each) to 0-based indices."""
    text = text.replace(",", " ").strip()
    if not text:
        return None
    parts = text.split()
    out: list[int] = []
    for p in parts:
        if not p.isdigit():
            return None
        v = int(p) - 1
        if v < 0 or v >= n:
            return None
        out.append(v)
    return out


# =============================================================================
# Auto policies
# =============================================================================

def auto_decision(
    paths: list[Path],
    policy: str,
    prefer_path: Path | None,
) -> tuple[int, list[int]]:
    """Return (keep_index, delete_indices) for one duplicate group."""
    n = len(paths)
    if policy == "keep-newest":
        keep = max(range(n), key=lambda i: _safe_mtime(paths[i]))
    elif policy == "keep-oldest":
        keep = min(range(n), key=lambda i: _safe_mtime(paths[i]))
    elif policy == "keep-shortest":
        keep = min(range(n), key=lambda i: (len(str(paths[i])), str(paths[i])))
    elif policy == "keep-in":
        if prefer_path is None:
            raise ValueError("keep-in policy requires --prefer-path")
        prefer_abs = prefer_path.resolve()
        scored = []
        for i, p in enumerate(paths):
            try:
                p.resolve().relative_to(prefer_abs)
                scored.append((0, i))
            except ValueError:
                scored.append((1, i))
        scored.sort()
        keep = scored[0][1]
    else:
        raise ValueError(f"unknown policy: {policy}")
    deletes = [i for i in range(n) if i != keep]
    return keep, deletes


def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


# =============================================================================
# Safety helpers
# =============================================================================

def path_is_under_any(path: Path, roots: Iterable[Path]) -> bool:
    """Return True when path is inside any root, after resolving paths."""
    try:
        path_abs = path.resolve()
    except OSError:
        path_abs = path.absolute()
    for root in roots:
        try:
            root_abs = Path(root).resolve()
        except OSError:
            root_abs = Path(root).absolute()
        try:
            path_abs.relative_to(root_abs)
            return True
        except ValueError:
            continue
    return False


def protected_indices(paths: list[Path], protected_roots: Iterable[Path]) -> set[int]:
    return {
        i for i, path in enumerate(paths)
        if path_is_under_any(path, protected_roots)
    }


@dataclass
class DeletionCandidate:
    path: Path
    size: int
    mtime_ts: float | None = None
    sha256: str | None = None


def validate_deletion_candidates(
    candidates: Iterable[DeletionCandidate],
    protected_roots: Iterable[Path] = (),
) -> tuple[list[tuple[Path, int]], list[str]]:
    """Preflight destructive actions before quarantine/permanent delete.

    A candidate is accepted only if it is not protected, still exists, has the
    expected size, and matches the expected hash when one is supplied.
    """
    ready: list[tuple[Path, int]] = []
    errors: list[str] = []
    protected = list(protected_roots)

    for candidate in candidates:
        path = candidate.path
        if path_is_under_any(path, protected):
            errors.append(f"{path} — protected path; skipped")
            continue
        try:
            st = path.stat()
        except OSError as e:
            errors.append(f"{path} — changed before execution: {e}")
            continue
        if st.st_size != candidate.size:
            errors.append(
                f"{path} — size changed before execution "
                f"({candidate.size} -> {st.st_size}); skipped"
            )
            continue
        if candidate.sha256:
            actual_hash = hash_file(path, partial=False)
            if actual_hash is None:
                errors.append(f"{path} — could not re-read before execution; skipped")
                continue
            if actual_hash != candidate.sha256:
                errors.append(f"{path} — content changed before execution; skipped")
                continue
        elif candidate.mtime_ts is not None and abs(st.st_mtime - candidate.mtime_ts) > 0.000001:
            errors.append(f"{path} — modified time changed before execution; skipped")
            continue
        ready.append((path, candidate.size))

    return ready, errors


# =============================================================================
# Execution: quarantine / permanent / restore
# =============================================================================

def _quarantine_relpath(orig: Path) -> Path:
    s = str(orig)
    if os.name == "nt" and len(s) >= 2 and s[1] == ":":
        s = s[0] + s[2:]
    s = s.lstrip("\\/")
    return Path(s)


def quarantine_files(
    deletions: list[tuple[Path, int]],
    quarantine_root: Path,
) -> tuple[int, int, list[dict], list[str]]:
    """Move files to quarantine. Returns (count, bytes_freed, manifest, errors)."""
    quarantine_root.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    errors: list[str] = []
    count = 0
    freed = 0
    for orig, size in deletions:
        rel = _quarantine_relpath(orig)
        dest = quarantine_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Handle collisions inside the quarantine (very rare; same path scanned twice).
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            k = 1
            while True:
                candidate = dest.with_name(f"{stem}.{k}{suffix}")
                if not candidate.exists():
                    dest = candidate
                    break
                k += 1
        try:
            shutil.move(str(orig), str(dest))
            manifest.append({
                "original": str(orig),
                "quarantined": str(dest),
                "size_bytes": size,
            })
            count += 1
            freed += size
        except OSError as e:
            errors.append(f"{orig} — {e}")
    if manifest:
        (quarantine_root / "manifest.json").write_text(
            json.dumps({
                "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "entries": manifest,
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return count, freed, manifest, errors


def permanent_delete_files(
    deletions: list[tuple[Path, int]],
) -> tuple[int, int, list[str]]:
    """Permanently delete files. Returns (count, bytes_freed, errors)."""
    errors: list[str] = []
    count = 0
    freed = 0
    for orig, size in deletions:
        try:
            orig.unlink()
            count += 1
            freed += size
        except OSError as e:
            errors.append(f"{orig} — {e}")
    return count, freed, errors


def restore_from_manifest(manifest_path: Path) -> int:
    """Restore files from a manifest. Returns 0 on success, non-zero on errors."""
    if manifest_path.is_dir():
        manifest_path = manifest_path / "manifest.json"
    if not manifest_path.is_file():
        print(f"error: manifest not found: {manifest_path}", file=sys.stderr)
        return 2
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot read manifest: {e}", file=sys.stderr)
        return 2

    entries = data.get("entries", [])
    if not entries:
        print("manifest is empty; nothing to restore.")
        return 0

    print(f"Restoring {len(entries)} file(s) from {manifest_path}")
    restored = 0
    skipped = 0
    errors = 0
    for entry in entries:
        orig = Path(entry["original"])
        quar = Path(entry["quarantined"])
        if not quar.exists():
            print(f"  skip (quarantine file missing): {quar}", file=sys.stderr)
            skipped += 1
            continue
        if orig.exists():
            print(f"  skip (original path occupied): {orig}", file=sys.stderr)
            skipped += 1
            continue
        try:
            orig.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(quar), str(orig))
            restored += 1
        except OSError as e:
            print(f"  error restoring {orig}: {e}", file=sys.stderr)
            errors += 1
    print(f"Restored {restored}, skipped {skipped}, errors {errors}.")
    return 0 if errors == 0 else 1


def list_quarantine_runs(quarantine_base: Path) -> list[dict]:
    """Return summaries for quarantine manifests, newest first."""
    if not quarantine_base.is_dir():
        return []

    runs: list[dict] = []
    for manifest_path in quarantine_base.glob("run-*/manifest.json"):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        entries = data.get("entries", [])
        total_bytes = 0
        available = 0
        restored_or_missing = 0
        for entry in entries:
            try:
                total_bytes += int(entry.get("size_bytes") or 0)
            except (TypeError, ValueError):
                pass
            quarantined_raw = entry.get("quarantined")
            if quarantined_raw and Path(quarantined_raw).exists():
                available += 1
            else:
                restored_or_missing += 1

        runs.append({
            "created_utc": data.get("created_utc") or "",
            "manifest_path": str(manifest_path),
            "quarantine_dir": str(manifest_path.parent),
            "file_count": len(entries),
            "available_count": available,
            "restored_or_missing_count": restored_or_missing,
            "total_bytes": total_bytes,
        })

    runs.sort(key=lambda r: (r["created_utc"], r["manifest_path"]), reverse=True)
    return runs


# =============================================================================
# Decision building (interactive + auto)
# =============================================================================

@dataclass
class Decision:
    size: int
    paths: list[Path]
    keep_index: int
    delete_indices: list[int]


def build_decisions_interactive(
    groups: list[tuple[int, list[Path]]],
    input_fn=input,
) -> list[Decision]:
    decisions: list[Decision] = []
    apply_default_to_rest = False
    total = len(groups)

    for i, (size, paths) in enumerate(groups, 1):
        if apply_default_to_rest:
            decisions.append(Decision(size, paths, 0, list(range(1, len(paths)))))
            continue

        anchor = common_ancestor(paths)
        shorts = short_paths(paths, anchor)
        print(f"\nGroup {i}/{total} — {format_size(size)} × {len(paths)} copies "
              f"= {format_size(size * (len(paths) - 1))} wasted")
        if anchor is not None:
            print(f"  (under {anchor})")
        for idx, (p, s) in enumerate(zip(paths, shorts), 1):
            marker = "[K]" if idx == 1 else "[-]"
            print(f"  {idx}) {marker} {s}  ({format_mtime(p)})")

        while True:
            try:
                raw = input_fn(
                    "  action: [Enter]=keep #1 + delete rest | "
                    "k N | d N,M | s=skip | a=apply to all | q=quit > "
                )
            except (EOFError, KeyboardInterrupt):
                print()
                return decisions
            action = parse_action(raw, len(paths))
            if action is None:
                print("  ?? unrecognized — try again (e.g. '', 'k 2', 'd 1,3', 's', 'a', 'q')")
                continue
            if action.kind == "quit":
                return decisions
            if action.kind == "skip":
                break
            if action.kind == "apply_all":
                apply_default_to_rest = True
                decisions.append(Decision(size, paths, 0, list(range(1, len(paths)))))
                break
            if action.kind == "execute":
                decisions.append(Decision(size, paths,
                                          action.keep_index,
                                          action.delete_indices))
                break
    return decisions


def build_decisions_auto(
    groups: list[tuple[int, list[Path]]],
    policy: str,
    prefer_path: Path | None,
) -> list[Decision]:
    out: list[Decision] = []
    for size, paths in groups:
        keep, deletes = auto_decision(paths, policy, prefer_path)
        out.append(Decision(size, paths, keep, deletes))
    return out


# =============================================================================
# Reporting
# =============================================================================

def print_report(groups: list[tuple[int, list[Path]]]) -> None:
    if not groups:
        print("No duplicates found.")
        return
    wasted = 0
    print(f"Found {len(groups)} duplicate group(s):\n")
    for i, (size, paths) in enumerate(groups, 1):
        group_waste = size * (len(paths) - 1)
        wasted += group_waste
        print(f"[{i}] {len(paths)} copies × {format_size(size)} "
              f"= {format_size(group_waste)} wasted")
        for p in paths:
            print(f"    {p}")
        print()
    print(f"Total wasted space: {format_size(wasted)}")


def report_as_json(groups: list[tuple[int, list[Path]]]) -> str:
    payload = {
        "groups": [
            {
                "size_bytes": size,
                "wasted_bytes": size * (len(paths) - 1),
                "paths": [str(p) for p in paths],
            }
            for size, paths in groups
        ],
        "total_wasted_bytes": sum(s * (len(p) - 1) for s, p in groups),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def summarize_plan(decisions: list[Decision]) -> tuple[int, int]:
    """Returns (file_count, bytes_to_free)."""
    n = sum(len(d.delete_indices) for d in decisions)
    b = sum(d.size * len(d.delete_indices) for d in decisions)
    return n, b


# =============================================================================
# main
# =============================================================================

def _confirm(prompt: str, input_fn=input) -> bool:
    try:
        ans = input_fn(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes")


def _execute_decisions(
    decisions: list[Decision],
    permanent: bool,
    quarantine_root: Path,
) -> None:
    deletions: list[tuple[Path, int]] = []
    for d in decisions:
        for idx in d.delete_indices:
            deletions.append((d.paths[idx], d.size))
    if not deletions:
        print("Nothing to do.")
        return

    if permanent:
        count, freed, errs = permanent_delete_files(deletions)
        print(f"\nPermanently deleted {count} file(s), freed {format_size(freed)}.")
    else:
        count, freed, manifest, errs = quarantine_files(deletions, quarantine_root)
        print(f"\nQuarantined {count} file(s) into {quarantine_root}")
        print(f"Freed {format_size(freed)}.")
        if manifest:
            print(f"Manifest: {quarantine_root / 'manifest.json'}")
            print(f"Restore with: python dupfinder.py --restore \"{quarantine_root}\"")
    for e in errs:
        print(f"  error: {e}", file=sys.stderr)


def _run_find_empty(args, skip: list[str]) -> int:
    ignore_junk = not args.keep_junk
    print(f"Scanning for empty folders in {len(args.paths)} root(s)...", file=sys.stderr)
    empties = find_empty_folders(args.paths, skip, ignore_junk=ignore_junk)

    if args.json:
        print(json.dumps({"empty_folders": [str(p) for p in empties]},
                         indent=2, ensure_ascii=False))
    else:
        if not empties:
            print("No empty folders found.")
        else:
            print(f"Found {len(empties)} empty folder(s)"
                  f"{' (ignoring junk metadata)' if ignore_junk else ''}:\n")
            for p in empties:
                print(f"  {p}")
            print()

    if not args.delete_empty or not empties:
        return 0

    if not args.yes:
        if not _confirm(f"\nDelete all {len(empties)} folder(s)? [y/N] "):
            print("Aborted.")
            return 0

    count, errors = delete_empty_folders(empties)
    print(f"\nDeleted {count} folder(s).")
    for e in errors:
        print(f"  error: {e}", file=sys.stderr)
    return 0 if not errors else 1


def _run_largest(args, skip: list[str]) -> int:
    print(f"Scanning largest items in {len(args.paths)} root(s)...", file=sys.stderr)
    files, folders, total_files, total_bytes = compute_top_largest(
        args.paths, skip,
        min_size=args.min_size,
        top_files=args.top_files,
        top_folders=args.top_folders,
    )

    if args.json:
        print(json.dumps({
            "files": [{"path": str(f.path), "size": f.size,
                       "mtime_ts": f.mtime_ts} for f in files],
            "folders": [{"path": str(f.path), "total_size": f.total_size,
                         "direct_size": f.direct_size,
                         "file_count": f.file_count} for f in folders],
            "total_files": total_files,
            "total_bytes": total_bytes,
        }, indent=2, ensure_ascii=False))
        return 0

    print(f"\nScanned {total_files} files, {format_size(total_bytes)} total.\n")
    if folders:
        print(f"Top {len(folders)} folder(s) by recursive size:")
        for i, f in enumerate(folders, 1):
            print(f"  [{i:>3}] {format_size(f.total_size):>10} · "
                  f"{f.file_count:>5} files · {f.path}")
        print()
    if files:
        print(f"Top {len(files)} file(s):")
        for i, f in enumerate(files, 1):
            print(f"  [{i:>3}] {format_size(f.size):>10} · {f.path}")
    return 0


def _run_similar_images(args, skip: list[str]) -> int:
    if not pillow_available():
        print("error: --similar-images requires Pillow. Install with: "
              "pip install Pillow", file=sys.stderr)
        return 2
    print(f"Scanning for visually similar images in {len(args.paths)} root(s) "
          f"(threshold = {args.similarity_threshold})...", file=sys.stderr)

    def show(stage, cur, total):
        print(f"  {stage}: {cur}/{total}", file=sys.stderr)

    groups, scanned, skipped = find_similar_images(
        args.paths, skip,
        threshold=args.similarity_threshold,
        min_size=args.min_size,
        progress=show,
    )

    if args.json:
        print(json.dumps({
            "groups": [
                [{
                    "path": str(im.path), "size": im.size, "mtime_ts": im.mtime_ts,
                    "width": im.width, "height": im.height, "dhash": f"{im.dhash:016x}",
                } for im in g]
                for g in groups
            ],
            "scanned": scanned,
            "skipped": skipped,
        }, indent=2, ensure_ascii=False))
        return 0

    if not groups:
        print(f"\nNo similar image groups found ({scanned} images scanned, "
              f"{skipped} skipped).")
        return 0

    wasted = sum(sum(im.size for im in g[1:]) for g in groups)
    print(f"\nFound {len(groups)} similar group(s), {format_size(wasted)} "
          f"in non-largest copies. ({scanned} scanned, {skipped} skipped)")
    for i, g in enumerate(groups, 1):
        print(f"\n[{i}] {len(g)} images:")
        for im in g:
            print(f"    {format_size(im.size):>10}  "
                  f"{im.width}x{im.height}  {im.path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sift",
        description="Sift — find duplicates, similar photos, empty folders, "
                    "and large files safely.",
    )
    parser.add_argument("paths", nargs="*", type=Path,
                        help="One or more directories to scan.")
    parser.add_argument("--delete", action="store_true",
                        help="Enter interactive per-group menu after scanning.")
    parser.add_argument("--auto", choices=[
        "keep-newest", "keep-oldest", "keep-shortest", "keep-in",
    ], help="Decide automatically per group, no prompts.")
    parser.add_argument("--prefer-path", type=Path,
                        help="Required with --auto keep-in: keep files under this directory.")
    parser.add_argument("--permanent", action="store_true",
                        help="Actually delete instead of moving to quarantine.")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip the final confirmation prompt.")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON report instead of text.")
    parser.add_argument("--min-size", type=int, default=1,
                        help="Ignore files smaller than this many bytes (default: 1).")
    parser.add_argument("--skip", action="append", default=[],
                        metavar="PATTERN",
                        help="fnmatch pattern to skip (file or directory name).")
    parser.add_argument("--hash-cache", action="store_true",
                        help="Cache SHA-256 hashes in .dupfinder_cache.sqlite3 "
                             "for faster repeated scans.")
    parser.add_argument("--cache-path", type=Path,
                        help="With --hash-cache: custom SQLite cache path.")
    parser.add_argument("--restore", type=Path, metavar="PATH",
                        help="Restore files from a previous quarantine manifest. "
                             "PATH may be the manifest.json or its containing folder.")
    parser.add_argument("--find-empty", action="store_true",
                        help="Find empty folders instead of duplicate files.")
    parser.add_argument("--delete-empty", action="store_true",
                        help="With --find-empty: delete the found folders "
                             "(asks for confirmation unless --yes).")
    parser.add_argument("--keep-junk", action="store_true",
                        help="With --find-empty: don't treat Thumbs.db/.DS_Store/"
                             "desktop.ini as 'effectively empty'.")
    parser.add_argument("--largest", action="store_true",
                        help="Find largest files and folders instead of duplicates.")
    parser.add_argument("--top-files", type=int, default=200,
                        help="With --largest: how many top files to list (default 200).")
    parser.add_argument("--top-folders", type=int, default=50,
                        help="With --largest: how many top folders to list (default 50).")
    parser.add_argument("--similar-images", action="store_true",
                        help="Find visually similar photos via perceptual hash. "
                             "Requires Pillow.")
    parser.add_argument("--similarity-threshold", type=int,
                        default=DEFAULT_SIMILARITY_THRESHOLD,
                        help=f"With --similar-images: max Hamming distance for the dHash "
                             f"to count as similar (default {DEFAULT_SIMILARITY_THRESHOLD}; "
                             "lower = stricter; useful range 0-15).")
    args = parser.parse_args(argv)

    if args.restore is not None:
        return restore_from_manifest(args.restore)

    if not args.paths:
        parser.error("at least one PATH is required (or use --restore)")
    if args.delete and args.auto:
        parser.error("--delete and --auto are mutually exclusive")
    if args.auto == "keep-in" and args.prefer_path is None:
        parser.error("--auto keep-in requires --prefer-path")

    for p in args.paths:
        if not p.is_dir():
            print(f"error: not a directory: {p}", file=sys.stderr)
            return 2

    skip = list(args.skip) + DEFAULT_SKIP

    if args.find_empty:
        return _run_find_empty(args, skip)
    if args.largest:
        return _run_largest(args, skip)
    if args.similar_images:
        return _run_similar_images(args, skip)

    print(f"Scanning {len(args.paths)} root(s)...", file=sys.stderr)
    entries = list(iter_files(args.paths, skip, args.min_size))
    print(f"  found {len(entries)} candidate files", file=sys.stderr)

    size_groups = group_by_size(entries)
    print(f"  {len(size_groups)} size group(s) with potential duplicates",
          file=sys.stderr)

    cache = None
    if args.hash_cache:
        cache = HashCache(args.cache_path or (app_data_dir() / HASH_CACHE_FILENAME))
    try:
        duplicates = find_duplicates(size_groups, cache=cache)
    finally:
        if cache is not None:
            stats = cache.stats()
            print(
                f"  hash cache: {stats['cache_hits']} hit(s), "
                f"{stats['cache_misses']} miss(es), "
                f"{stats['cache_writes']} write(s)",
                file=sys.stderr,
            )
            cache.close()

    if args.json:
        print(report_as_json(duplicates))
    else:
        print_report(duplicates)

    if not duplicates:
        return 0
    if not (args.delete or args.auto):
        return 0

    # Build decisions.
    if args.auto:
        decisions = build_decisions_auto(duplicates, args.auto, args.prefer_path)
    else:
        if not sys.stdin.isatty():
            print("error: --delete requires an interactive terminal; "
                  "use --auto for non-interactive runs.", file=sys.stderr)
            return 2
        decisions = build_decisions_interactive(duplicates)

    # Summary + confirmation.
    n_files, n_bytes = summarize_plan(decisions)
    if n_files == 0:
        print("\nNothing selected.")
        return 0

    mode = "PERMANENTLY DELETE" if args.permanent else "move to quarantine"
    print(f"\nPlan: {mode} {n_files} file(s), freeing {format_size(n_bytes)}.")
    if not args.yes:
        if not _confirm("Proceed? [y/N] "):
            print("Aborted.")
            return 0

    ts = time.strftime("%Y%m%d-%H%M%S")
    quarantine_root = app_data_dir() / QUARANTINE_DIRNAME / f"run-{ts}"
    _execute_decisions(decisions, args.permanent, quarantine_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
