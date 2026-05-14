"""dupfinder_gui — Tkinter front-end for dupfinder.

Run: python dupfinder_gui.py

Wraps dupfinder.py's core scan/decision/execute logic in a GUI:
    1. Pick one or more folders.
    2. Hit Scan; results appear grouped in a tree.
    3. Double-click a file to mark it as the one to KEEP in its group.
    4. Or apply an auto policy (newest / oldest / shortest path) to all groups.
    5. Hit Execute to quarantine (or permanently delete) the marked-for-delete copies.
    6. Restore... opens a previous quarantine manifest and undoes everything.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

import dupfinder as df


# =============================================================================
# Background scan worker
# =============================================================================

@dataclass
class ScanProgress:
    kind: str  # "status" | "done" | "error"
    text: str = ""
    duplicates: Optional[list[tuple[int, list[Path]]]] = None


def _scan_worker(
    roots: list[Path],
    skip_patterns: list[str],
    min_size: int,
    out_q: queue.Queue,
) -> None:
    try:
        out_q.put(ScanProgress("status", "Walking files..."))
        entries = list(df.iter_files(roots, skip_patterns, min_size))
        out_q.put(ScanProgress("status", f"Found {len(entries)} candidates; grouping by size..."))
        size_groups = df.group_by_size(entries)
        out_q.put(ScanProgress(
            "status",
            f"{len(size_groups)} size group(s) need hashing; this is the slow part..."
        ))
        duplicates = df.find_duplicates(size_groups)
        out_q.put(ScanProgress("done", "", duplicates))
    except Exception as e:  # noqa: BLE001
        out_q.put(ScanProgress("error", f"{type(e).__name__}: {e}"))


# =============================================================================
# Main app
# =============================================================================

class DupFinderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("dupfinder")
        root.geometry("960x640")
        root.minsize(720, 480)

        self.folders: list[Path] = []
        self.duplicates: list[tuple[int, list[Path]]] = []
        self.keep_choice: dict[int, int] = {}   # group_idx -> kept file_idx
        self.scan_queue: queue.Queue[ScanProgress] = queue.Queue()
        self.scan_thread: Optional[threading.Thread] = None
        self.last_quarantine: Optional[Path] = None

        self._build_ui()
        self._refresh_status("Ready. Add a folder and click Scan.")

    # -------------------------------------------------------------------------
    # Layout
    # -------------------------------------------------------------------------

    def _build_ui(self) -> None:
        pad = {"padx": 6, "pady": 4}

        # Top: folders list -----------------------------------------------------
        top = ttk.LabelFrame(self.root, text="Folders to scan")
        top.pack(fill="x", **pad)
        self.folder_list = tk.Listbox(top, height=4, activestyle="none")
        self.folder_list.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        btns = ttk.Frame(top)
        btns.pack(side="right", fill="y", padx=4, pady=4)
        ttk.Button(btns, text="Add folder...", command=self._add_folder).pack(fill="x")
        ttk.Button(btns, text="Remove", command=self._remove_folder).pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="Clear", command=self._clear_folders).pack(fill="x", pady=(4, 0))

        # Options --------------------------------------------------------------
        opts = ttk.LabelFrame(self.root, text="Options")
        opts.pack(fill="x", **pad)
        ttk.Label(opts, text="Min size (bytes):").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.min_size_var = tk.StringVar(value="1")
        ttk.Entry(opts, textvariable=self.min_size_var, width=10).grid(row=0, column=1, sticky="w")
        ttk.Label(opts, text="Skip patterns (comma-separated):").grid(row=0, column=2, sticky="w", padx=(16, 4))
        self.skip_var = tk.StringVar(value="")
        ttk.Entry(opts, textvariable=self.skip_var, width=30).grid(row=0, column=3, sticky="ew")
        opts.columnconfigure(3, weight=1)
        self.permanent_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opts,
            text="PERMANENT delete (skip quarantine — irrecoverable)",
            variable=self.permanent_var,
            command=self._on_permanent_toggle,
        ).grid(row=1, column=0, columnspan=4, sticky="w", padx=4, pady=4)

        # Actions --------------------------------------------------------------
        actions = ttk.Frame(self.root)
        actions.pack(fill="x", **pad)
        self.scan_btn = ttk.Button(actions, text="Scan", command=self._start_scan)
        self.scan_btn.pack(side="left")
        ttk.Label(actions, text="   Auto policy:").pack(side="left")
        self.policy_var = tk.StringVar(value="keep-newest")
        ttk.Combobox(
            actions, textvariable=self.policy_var, state="readonly", width=16,
            values=["keep-newest", "keep-oldest", "keep-shortest"],
        ).pack(side="left", padx=4)
        self.apply_btn = ttk.Button(actions, text="Apply policy",
                                    command=self._apply_policy, state="disabled")
        self.apply_btn.pack(side="left", padx=4)
        self.execute_btn = ttk.Button(actions, text="Execute",
                                      command=self._execute, state="disabled")
        self.execute_btn.pack(side="left", padx=4)
        ttk.Button(actions, text="Restore...", command=self._restore_dialog).pack(side="right")

        # Results tree ---------------------------------------------------------
        tree_frame = ttk.LabelFrame(self.root, text="Duplicates")
        tree_frame.pack(fill="both", expand=True, **pad)
        self.tree = ttk.Treeview(
            tree_frame,
            columns=("size", "mtime"),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="Group / Path")
        self.tree.heading("size", text="Size")
        self.tree.heading("mtime", text="Modified")
        self.tree.column("#0", width=560, stretch=True)
        self.tree.column("size", width=110, anchor="e", stretch=False)
        self.tree.column("mtime", width=150, anchor="w", stretch=False)
        self.tree.tag_configure("keep", foreground="#0a7d20")
        self.tree.tag_configure("delete", foreground="#b00020")
        self.tree.tag_configure("group", font=("TkDefaultFont", 9, "bold"))
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Return>", self._on_tree_double_click)

        # Status bar -----------------------------------------------------------
        status = ttk.Frame(self.root)
        status.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="")
        ttk.Label(status, textvariable=self.status_var, anchor="w",
                  relief="sunken", padding=(6, 2)).pack(fill="x")

    # -------------------------------------------------------------------------
    # Folder management
    # -------------------------------------------------------------------------

    def _add_folder(self) -> None:
        path = filedialog.askdirectory(title="Pick a folder to scan")
        if not path:
            return
        p = Path(path)
        if p in self.folders:
            return
        self.folders.append(p)
        self.folder_list.insert("end", str(p))

    def _remove_folder(self) -> None:
        sel = self.folder_list.curselection()
        if not sel:
            return
        for idx in reversed(sel):
            del self.folders[idx]
            self.folder_list.delete(idx)

    def _clear_folders(self) -> None:
        self.folders.clear()
        self.folder_list.delete(0, "end")

    def _on_permanent_toggle(self) -> None:
        if self.permanent_var.get():
            ok = messagebox.askyesno(
                "Confirm permanent mode",
                "Permanent mode will UNLINK files — they cannot be restored.\n\n"
                "Use this only if you really don't want a quarantine safety net.\n\n"
                "Proceed?",
                icon="warning",
            )
            if not ok:
                self.permanent_var.set(False)

    # -------------------------------------------------------------------------
    # Scanning
    # -------------------------------------------------------------------------

    def _start_scan(self) -> None:
        if self.scan_thread and self.scan_thread.is_alive():
            return
        if not self.folders:
            messagebox.showinfo("No folders", "Add at least one folder first.")
            return
        for f in self.folders:
            if not f.is_dir():
                messagebox.showerror("Bad path", f"Not a directory:\n{f}")
                return
        try:
            min_size = int(self.min_size_var.get() or "1")
            if min_size < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Bad value", "Min size must be a non-negative integer.")
            return

        skip = [s.strip() for s in self.skip_var.get().split(",") if s.strip()]
        skip += df.DEFAULT_SKIP

        self._clear_results()
        self.scan_btn.configure(state="disabled")
        self.apply_btn.configure(state="disabled")
        self.execute_btn.configure(state="disabled")
        self._refresh_status("Scanning...")

        self.scan_thread = threading.Thread(
            target=_scan_worker,
            args=(list(self.folders), skip, min_size, self.scan_queue),
            daemon=True,
        )
        self.scan_thread.start()
        self.root.after(100, self._poll_scan_queue)

    def _poll_scan_queue(self) -> None:
        try:
            while True:
                msg = self.scan_queue.get_nowait()
                if msg.kind == "status":
                    self._refresh_status(msg.text)
                elif msg.kind == "error":
                    messagebox.showerror("Scan failed", msg.text)
                    self.scan_btn.configure(state="normal")
                    self._refresh_status("Scan failed.")
                    return
                elif msg.kind == "done":
                    self.duplicates = msg.duplicates or []
                    self._populate_results()
                    self.scan_btn.configure(state="normal")
                    have = bool(self.duplicates)
                    self.apply_btn.configure(state="normal" if have else "disabled")
                    self.execute_btn.configure(state="normal" if have else "disabled")
                    return
        except queue.Empty:
            pass
        if self.scan_thread and self.scan_thread.is_alive():
            self.root.after(100, self._poll_scan_queue)

    # -------------------------------------------------------------------------
    # Results tree
    # -------------------------------------------------------------------------

    def _clear_results(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.duplicates = []
        self.keep_choice = {}

    def _populate_results(self) -> None:
        self.keep_choice = {g: 0 for g in range(len(self.duplicates))}
        if not self.duplicates:
            self._refresh_status("Scan complete: no duplicates found.")
            return

        for gi, (size, paths) in enumerate(self.duplicates):
            anchor = df.common_ancestor(paths)
            wasted = size * (len(paths) - 1)
            group_label = (f"Group {gi + 1} — {len(paths)} copies × {df.format_size(size)} "
                           f"= {df.format_size(wasted)} wasted"
                           + (f"   (under {anchor})" if anchor else ""))
            self.tree.insert("", "end", iid=f"g{gi}", text=group_label,
                             values=("", ""), tags=("group",), open=True)
            shorts = df.short_paths(paths, anchor)
            for fi, (p, label) in enumerate(zip(paths, shorts)):
                self.tree.insert(
                    f"g{gi}", "end", iid=f"f{gi}_{fi}",
                    text=self._file_label(gi, fi, label),
                    values=(df.format_size(size), df.format_mtime(p)),
                    tags=("keep" if fi == 0 else "delete",),
                )

        n = sum(len(p) - 1 for _, p in self.duplicates)
        b = sum(s * (len(p) - 1) for s, p in self.duplicates)
        self._refresh_status(
            f"Found {len(self.duplicates)} group(s); {n} file(s) "
            f"({df.format_size(b)}) marked for delete. "
            "Double-click any file to make it the keeper."
        )

    def _file_label(self, gi: int, fi: int, short: str) -> str:
        marker = "[K]" if self.keep_choice.get(gi, 0) == fi else "[-]"
        return f"  {marker} {short}"

    def _refresh_group(self, gi: int) -> None:
        size, paths = self.duplicates[gi]
        anchor = df.common_ancestor(paths)
        shorts = df.short_paths(paths, anchor)
        for fi, (_, label) in enumerate(zip(paths, shorts)):
            iid = f"f{gi}_{fi}"
            new_tag = "keep" if self.keep_choice.get(gi, 0) == fi else "delete"
            self.tree.item(iid, text=self._file_label(gi, fi, label), tags=(new_tag,))

    def _on_tree_double_click(self, _event: tk.Event) -> None:
        sel = self.tree.focus()
        if not sel or not sel.startswith("f"):
            return
        gi_str, fi_str = sel[1:].split("_", 1)
        gi, fi = int(gi_str), int(fi_str)
        self.keep_choice[gi] = fi
        self._refresh_group(gi)

    # -------------------------------------------------------------------------
    # Apply policy / execute
    # -------------------------------------------------------------------------

    def _apply_policy(self) -> None:
        policy = self.policy_var.get()
        if not self.duplicates:
            return
        for gi, (_, paths) in enumerate(self.duplicates):
            try:
                keep, _ = df.auto_decision(paths, policy, None)
            except ValueError as e:
                messagebox.showerror("Policy error", str(e))
                return
            self.keep_choice[gi] = keep
            self._refresh_group(gi)
        self._refresh_status(f"Applied policy '{policy}' to {len(self.duplicates)} group(s).")

    def _execute(self) -> None:
        if not self.duplicates:
            return
        decisions = []
        for gi, (size, paths) in enumerate(self.duplicates):
            keep = self.keep_choice.get(gi, 0)
            deletes = [i for i in range(len(paths)) if i != keep]
            decisions.append(df.Decision(size, paths, keep, deletes))

        n, b = df.summarize_plan(decisions)
        if n == 0:
            messagebox.showinfo("Nothing to do", "No files are marked for deletion.")
            return

        permanent = self.permanent_var.get()
        mode_text = "PERMANENTLY DELETE" if permanent else "move to quarantine"
        icon = "warning" if permanent else "question"
        ok = messagebox.askyesno(
            "Confirm",
            f"About to {mode_text} {n} file(s), freeing {df.format_size(b)}.\n\nProceed?",
            icon=icon,
        )
        if not ok:
            return

        deletions: list[tuple[Path, int]] = []
        for d in decisions:
            for idx in d.delete_indices:
                deletions.append((d.paths[idx], d.size))

        if permanent:
            count, freed, errors = df.permanent_delete_files(deletions)
            msg = f"Permanently deleted {count} file(s), freed {df.format_size(freed)}."
        else:
            ts = time.strftime("%Y%m%d-%H%M%S")
            qroot = Path.cwd() / df.QUARANTINE_DIRNAME / f"run-{ts}"
            count, freed, manifest, errors = df.quarantine_files(deletions, qroot)
            self.last_quarantine = qroot
            msg = (f"Quarantined {count} file(s), freed {df.format_size(freed)}.\n\n"
                   f"Manifest:\n{qroot / 'manifest.json'}\n\n"
                   "Use 'Restore...' to undo this.")

        if errors:
            msg += "\n\nSome errors occurred:\n" + "\n".join(errors[:10])
            if len(errors) > 10:
                msg += f"\n... and {len(errors) - 10} more"

        messagebox.showinfo("Done", msg)
        # Re-scan so the tree reflects what's left.
        self._clear_results()
        self.apply_btn.configure(state="disabled")
        self.execute_btn.configure(state="disabled")
        self._refresh_status("Done. Click Scan again to refresh.")

    # -------------------------------------------------------------------------
    # Restore
    # -------------------------------------------------------------------------

    def _restore_dialog(self) -> None:
        initial = str(Path.cwd() / df.QUARANTINE_DIRNAME) if (Path.cwd() / df.QUARANTINE_DIRNAME).is_dir() else None
        manifest = filedialog.askopenfilename(
            title="Pick a quarantine manifest",
            initialdir=initial,
            filetypes=[("Manifest", "manifest.json"), ("JSON", "*.json"), ("All", "*.*")],
        )
        if not manifest:
            return
        ok = messagebox.askyesno(
            "Confirm restore",
            f"Restore files listed in:\n{manifest}\n\n"
            "Files whose original path is now occupied will be skipped.\n\nProceed?",
        )
        if not ok:
            return
        rc = df.restore_from_manifest(Path(manifest))
        if rc == 0:
            messagebox.showinfo("Restore", "Restore completed.")
        else:
            messagebox.showwarning("Restore", "Restore finished with some errors. See console.")

    # -------------------------------------------------------------------------
    # Status helper
    # -------------------------------------------------------------------------

    def _refresh_status(self, text: str) -> None:
        self.status_var.set(text)


def main() -> int:
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.3)
    except tk.TclError:
        pass
    DupFinderApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
