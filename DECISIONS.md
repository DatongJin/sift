# Sift — Development Decisions

This document records the major design decisions made while building Sift.
For each decision: the options that were on the table, the choice we made,
and why.

> Note: Sift is a **local-only desktop application**. It deliberately does
> **not** use a cloud server. "Why not a cloud app" is itself decision #1
> below.

---

## 1. Product form factor

**The question:** What kind of program is Sift?

**Options considered:**

| Option | Pros | Cons |
| --- | --- | --- |
| Cloud web app (user uploads files) | Works in any browser, nothing to install | Users would have to upload tens of GB of personal photos to a stranger's server — privacy disaster, bandwidth cost, defeats the entire purpose |
| Pure browser app (File System Access API) | No install | Chromium-desktop only; re-grant folder permission every visit; would need to re-implement Pillow/SQLite in JS or ship a 30 MB Pyodide bundle |
| CLI only | Simplest to build | Non-technical users can't use it |
| Tkinter desktop GUI | Pure stdlib, no deps | Looks dated and ugly; hard to make modern |
| Electron app | Modern UI, cross-platform | 100+ MB binary, heavy toolchain, overkill |
| **Local HTTP server + browser UI** | Modern UI via HTML/CSS, browser is the render engine, zero frontend deps, files never leave the machine | Slightly unusual architecture; needs lifecycle care |

**Decision:** Local HTTP server (Python stdlib `http.server`) bound to
`127.0.0.1`, with a browser window as the UI.

**Why:** It gives a modern-looking UI without a frontend framework or build
step, keeps all file processing local (privacy), and reuses the browser as a
free, capable rendering engine. The cloud and pure-browser approaches were
rejected because Sift's whole job — recursively walking a user's disk — is
something browsers deliberately forbid and something users would never upload.

---

## 2. Duplicate detection strategy

**The question:** How do we decide two files are duplicates?

**Options considered:**

- Filename match — fast but wrong (same name ≠ same content; different name can be identical)
- Hash every file fully — correct but reads every byte of every file
- Size grouping then hash — better
- **Three-tier filter: size → first 8 KB partial hash → full SHA-256**

**Decision:** Three-tier filter.

**Why:** Files of different size cannot be duplicates (free `stat` filter).
Among same-size files, an 8 KB partial hash eliminates most non-matches
cheaply. Only files that survive both get a full hash. On real data this
skips 95 %+ of the I/O a naive full-hash approach would do.

---

## 3. Hash function

**Options considered:** MD5, SHA-1, SHA-256, BLAKE3, xxHash.

**Decision:** SHA-256.

**Why:** It's in the Python standard library (no dependency), it's
collision-resistant (MD5/SHA-1 are not), and it's not the bottleneck — the
process is I/O-bound, so a faster hash like BLAKE3 or xxHash wouldn't change
wall-clock time but would add a dependency.

---

## 4. Similar-photo detection algorithm

**The question:** How do we find photos that look the same but aren't
byte-identical (burst shots, recompressed copies, slight crops)?

**Options considered:**

| Option | Notes |
| --- | --- |
| aHash (average hash) | Too sensitive to brightness changes |
| pHash (DCT-based) | Accurate but slower; needs a DCT pass |
| **dHash (difference hash)** | Fast, robust to resize/recompression, simple to implement, no heavy deps |
| Deep-learning embeddings | Most accurate but needs a model (100s of MB) + GPU ideally |
| SSIM | Pairwise structural comparison, O(n²) on full images — too slow |

**Decision:** dHash — shrink to 9×8 grayscale, compare adjacent pixels to
produce a 64-bit fingerprint; two images are similar if the Hamming distance
between fingerprints is ≤ a threshold (default 5).

**Why:** Best balance of speed, robustness, and simplicity. It catches the
real use case (same shot, different compression/size/crop) without a model
download or heavy math.

---

## 5. Image library

**Options considered:** Pillow, OpenCV, Wand (ImageMagick), pure stdlib.

**Decision:** Pillow, as an **optional** dependency.

**Why:** Pillow is the de-facto Python image library and ships with Anaconda.
OpenCV/Wand are heavier. We made it optional: the duplicate / empty-folder /
largest-items modes work with zero dependencies; only the similar-photo mode
needs Pillow, and the UI degrades gracefully if it's missing.

---

## 6. Photo-scan performance

**The question:** A folder of 1,700 DSLR JPEGs (≈ 85 GB) took ~15 minutes.
Too slow.

**Options considered:** naive full decode, multiprocessing, GPU, libjpeg
draft mode, thread pool, persistent cache.

**Decision:** Stack three optimizations:

1. `Image.draft()` — tells libjpeg to decode large JPEGs at 1/4 or 1/8 scale
   in the DCT domain (~10× faster, zero dHash accuracy loss — verified at 0
   bit difference).
2. Single decode per image — dHash, dimensions, and thumbnail all derived
   from one `Image.open()` instead of three.
3. `ThreadPoolExecutor` with 8 workers — Pillow releases the GIL during
   decode, so threads scale near-linearly.

**Why:** Combined ~30× speedup (15 min → ~30 s on the same folder) with no
new dependency and no accuracy loss. GPU and multiprocessing were rejected as
heavier than necessary.

---

## 7. Deletion safety model

**The question:** When the user removes files, what actually happens?

**Options considered:**

- Hard delete (`os.unlink`) — fast, irreversible, scary
- OS recycle bin — recoverable but needs platform-specific code and has size limits
- **Quarantine folder + JSON manifest + restore**

**Decision:** Move files into a timestamped `.dupfinder_quarantine/` folder,
write a JSON manifest, offer one-click restore. Permanent delete is opt-in.

**Why:** Fully reversible, transparent (the manifest is human-readable),
cross-platform (no OS-specific recycle-bin API), and it preserves the
original directory structure so restore is exact.

---

## 8. Web security (CSRF)

**The question:** The server listens on `127.0.0.1`; any web page in any
browser tab can POST to it. A malicious page could trigger file deletion.

**Options considered:** nothing, token-based auth, Origin/Host/Content-Type
checks.

**Decision:** Reject a POST unless: the `Host` header is loopback (defeats
DNS-rebinding), the `Origin` header (if present) matches our own URL, and the
`Content-Type` is `application/json` (forces a CORS preflight that we never
answer, blocking cross-origin `fetch`).

**Why:** Eliminates the CSRF attack surface with three header checks and zero
user-facing friction (no login). Token auth was rejected as unnecessary
friction for a local single-user tool.

---

## 9. Cancelling a running scan

**Options considered:** no cancel, kill the process, a `threading.Event`
signal threaded through the scan functions.

**Decision:** A `threading.Event` passed into `iter_files`, `hash_file`,
`find_duplicates`, `find_empty_folders`, and `compute_top_largest`, checked
before each file and inside the chunked hash-read loop.

**Why:** Graceful and responsive (≈ 50 ms to abort even mid-large-file)
without killing the process or leaving state inconsistent.

---

## 10. Handling long scans over HTTP

**The question:** A 15-minute scan held a single HTTP request open; the
browser eventually reported "Failed to fetch" when the OS dropped the idle
socket.

**Options considered:** synchronous request, WebSocket, background job +
status polling.

**Decision:** `/api/scan_*_start` creates a job and returns immediately; the
work runs on a background thread; the UI polls `/api/scan_status` every
~450 ms for progress and the final result.

**Why:** Survives arbitrarily long scans, gives a live progress bar, and
reuses the existing cancel mechanism. WebSocket was rejected as more
machinery than a poll loop needs for this.

---

## 11. Hash cache

**Options considered:** none, in-memory only, pickle file, SQLite.

**Decision:** SQLite (`.dupfinder_cache.sqlite3`), keyed by
`path + size + mtime + hash-mode`.

**Why:** Persists across runs so re-scanning the same folder is near-instant;
the composite key auto-invalidates when a file changes; SQLite is in the
standard library.

---

## 12. Packaging and distribution

**Options considered:** source only, pip/PyPI package, PyInstaller
`--onefile`, PyInstaller `--onedir`, Nuitka, cx_Freeze.

**Decision:** PyInstaller `--onefile` → a single `sift.exe`, also wrapped in
`sift-win64.zip` with a README.

**Why:** One file, no Python required on the target machine, double-click to
run. The zip wrapper matches what users expect from a "portable Windows
tool." pip was rejected because it requires the user to already have Python.

---

## 13. Shrinking the bundle

**The question:** The first PyInstaller build was **235 MB** because Anaconda
drags in pandas/numpy/scipy/matplotlib/etc.

**Options considered:** ship 235 MB, `--exclude-module` list, build from a
clean virtualenv.

**Decision:** An explicit `--exclude-module` list (numpy, pandas, scipy,
matplotlib, IPython, jupyter, sklearn, …).

**Why:** Dropped the build from 235 MB to **16 MB** (93 % smaller) with one
build-script change and no clean-environment setup.

---

## 14. Application lifecycle (window-close behavior)

**The question:** The exe started a server and opened a detached browser
window; closing the window left a phantom server running in the background.

**Options considered:** detached server, system-tray app, window-tied
lifecycle.

**Decision:** Launch Edge/Chrome `--app` with a dedicated `--user-data-dir`
in temp, track that process, and exit (shutting the server down + cleaning the
temp profile) when the user closes the window.

**Why:** Matches the expected desktop experience ("close window = quit"). The
dedicated `--user-data-dir` is essential — without it, `--app=URL` on a
machine that already has Edge open would just hand the URL to the existing
Edge process and our process handle would return instantly, making us think
the window had already closed.

---

## 15. Browser launcher

**Options considered:** `webbrowser.open` (default browser), Edge `--app`,
Chrome `--app`, pywebview (embedded WebView2), Electron.

**Decision:** Edge or Chrome in `--app` mode, falling back to the default
browser if neither is found.

**Why:** `--app` mode gives a chromeless, native-feeling window with no extra
dependency, and Windows 10/11 always ships with Edge. pywebview/Electron were
rejected as added weight for no real gain here.

---

## 16. Native folder picker

**The question:** The UI needs a real "choose folder" dialog. Browsers don't
expose absolute paths (privacy), so we need a native one.

**Options considered:** HTML file input, browser File System Access API,
`subprocess` + tkinter, direct tkinter in a thread, Win32 ctypes COM dialog.

**Decision:** Run tkinter's `filedialog` directly in a daemon thread, guarded
by a lock so two clicks can't race.

**Why:** The original design spawned a subprocess (`python -c "...tkinter..."`)
to avoid threading issues. That broke in the bundled exe because
`sys.executable` points at `sift.exe`, not a Python interpreter — so the
subprocess never ran a dialog. Direct tkinter-in-a-thread works in both
script mode and the frozen exe. Win32 ctypes was rejected as ~50 lines of COM
ceremony for no extra benefit.

---

## 17. Landing-page hosting

**Options considered:** GitHub Pages, Netlify, Vercel, Cloudflare Pages, own
server.

**Decision:** GitHub Pages, served from the `docs/` folder of the same repo.

**Why:** Free, zero extra accounts, auto-deploys on every push, and lives
next to the code. The others are equally free but add a second platform for
no benefit at this scale.

---

## 18. Repository visibility

**Options considered:** private, public.

**Decision:** Public (started private, then flipped).

**Why:** Free GitHub Pages and anonymous release downloads both require a
public repo. Sift has no secrets in it (tokens were always passed via stdin
or env and scrubbed; `.gitignore` excludes state files), so there is no cost
to being public and real benefit (downloads, landing page, sharing).

---

## 19. Internal module naming

**The question:** The product is "Sift" but the core module is still
`dupfinder.py`.

**Options considered:** rename everything to `sift.py`, keep `dupfinder.py`
internally.

**Decision:** Keep `dupfinder.py` (and `.dupfinder_quarantine/`,
`.dupfinder_cache.sqlite3`); only the user-facing strings say "Sift".

**Why:** Renaming the module ripples through every import, the test suite,
and — critically — the on-disk storage path names. Renaming the storage
paths would orphan any files a user already quarantined. The cosmetic gain of
an internal rename isn't worth that risk; users never see the module name.

---

## 20. License

**Options considered:** MIT, Apache 2.0, GPL v3, none.

**Decision:** None for now (deferred to the owner).

**Why:** The repo started private; a license can be added before any serious
public promotion. Owner's call — documented here so the gap is intentional,
not forgotten.

---

## Things explicitly NOT done (and why)

- **Mac / Linux builds** — PyInstaller can't cross-compile; would need a Mac
  to build the Mac binary, plus ~$99/yr Apple Developer for signing. Deferred.
- **Android / iPhone** — not a port; mobile sandboxing makes Sift's
  "walk-the-disk" model impossible. A mobile photo-dedup app would be a
  separate product in Swift/Kotlin.
- **Code signing** — ~$300/yr; not worth it until there's download traction.
  Users currently click through the Windows SmartScreen warning.
- **Auto-update** — premature at v0.1.0.
- **Telemetry / crash reporting** — conflicts with the local-only,
  no-network privacy promise.
