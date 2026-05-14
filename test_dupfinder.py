"""Tests for dupfinder. Run with: python test_dupfinder.py"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import dupfinder as df


class DupFinderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        df.reset_app_data_dir_cache()

    def _write(self, rel: str, data: bytes, mtime: float | None = None) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    # ---------- format_size ----------
    def test_format_size_units(self) -> None:
        self.assertEqual(df.format_size(0), "0 B")
        self.assertEqual(df.format_size(500), "500 B")
        self.assertTrue(df.format_size(2048).startswith("2.00 KB"))
        self.assertTrue(df.format_size(5 * 1024 * 1024).endswith("MB"))

    # ---------- _matches / skip ----------
    def test_matches_patterns(self) -> None:
        self.assertTrue(df._matches("foo.tmp", ["*.tmp"]))
        self.assertFalse(df._matches("foo.txt", ["*.tmp"]))
        self.assertTrue(df._matches("node_modules", ["node_modules"]))

    # ---------- iter_files ----------
    def test_iter_files_respects_min_size_and_skip(self) -> None:
        self._write("a.txt", b"hello")
        self._write("b.tmp", b"hello world")
        self._write("sub/c.txt", b"x")
        self._write("sub/d.txt", b"hello world ok")
        entries = list(df.iter_files([self.root], ["*.tmp"], min_size=2))
        names = sorted(p.name for p, _, _ in entries)
        self.assertEqual(names, ["a.txt", "d.txt"])

    def test_iter_files_skips_directory(self) -> None:
        self._write("keep/a.bin", b"AAA")
        self._write("node_modules/lib/b.bin", b"BBB")
        entries = list(df.iter_files([self.root], ["node_modules"], min_size=1))
        names = [p.name for p, _, _ in entries]
        self.assertEqual(names, ["a.bin"])

    # ---------- group_by_size ----------
    def test_group_by_size_filters_singletons(self) -> None:
        e = [
            (Path("a"), 10, (1, 1)),
            (Path("b"), 10, (1, 2)),
            (Path("c"), 20, (1, 3)),
        ]
        groups = df.group_by_size(e)
        self.assertEqual(set(groups.keys()), {10})
        self.assertEqual(len(groups[10]), 2)

    # ---------- hash_file ----------
    def test_hash_file_full_vs_partial(self) -> None:
        a = self._write("a.bin", b"X" * (df.PARTIAL_BYTES * 2))
        b = self._write("b.bin", b"X" * df.PARTIAL_BYTES + b"Y" * df.PARTIAL_BYTES)
        self.assertEqual(df.hash_file(a, partial=True), df.hash_file(b, partial=True))
        self.assertNotEqual(df.hash_file(a, partial=False), df.hash_file(b, partial=False))

    def test_hash_file_missing_returns_none(self) -> None:
        self.assertIsNone(df.hash_file(self.root / "ghost", partial=False))

    def test_hash_cache_reuses_unchanged_hash(self) -> None:
        f = self._write("cached.bin", b"cached content")
        cache = df.HashCache(self.root / "hashes.sqlite3")
        try:
            first = df.hash_file(f, partial=False, cache=cache)
            stats_after_first = cache.stats()
            second = df.hash_file(f, partial=False, cache=cache)
            stats_after_second = cache.stats()
        finally:
            cache.close()
        self.assertEqual(first, second)
        self.assertEqual(stats_after_first["cache_hits"], 0)
        self.assertEqual(stats_after_first["cache_misses"], 1)
        self.assertEqual(stats_after_first["cache_writes"], 1)
        self.assertEqual(stats_after_second["cache_hits"], 1)

    def test_find_duplicates_uses_hash_cache(self) -> None:
        self._write("a.txt", b"same cache")
        self._write("b.txt", b"same cache")
        entries = list(df.iter_files([self.root], [], min_size=1))
        groups = df.group_by_size(entries)
        cache = df.HashCache(self.root / "hashes.sqlite3")
        try:
            df.find_duplicates(groups, cache=cache)
            first = cache.stats()
            df.find_duplicates(groups, cache=cache)
            second = cache.stats()
        finally:
            cache.close()
        self.assertGreater(first["cache_writes"], 0)
        self.assertGreater(second["cache_hits"], first["cache_hits"])

    # ---------- find_duplicates ----------
    def test_find_duplicates_basic(self) -> None:
        content = b"same content here"
        self._write("dup1.txt", content)
        self._write("nested/dup2.txt", content)
        self._write("unique.txt", b"a totally other length here right!")
        self.assertEqual(len(content), 17)
        self._write("same_size_diff.txt", b"abcdefghijklmnopq")

        entries = list(df.iter_files([self.root], [], min_size=1))
        size_groups = df.group_by_size(entries)
        self.assertEqual(len(size_groups[17]), 3)
        dups = df.find_duplicates(size_groups)
        self.assertEqual(len(dups), 1)
        size, paths = dups[0]
        self.assertEqual(size, 17)
        self.assertEqual({p.name for p in paths}, {"dup1.txt", "dup2.txt"})

    def test_find_duplicates_partial_hash_filters_out(self) -> None:
        prefix = b"P" * df.PARTIAL_BYTES
        self._write("a.bin", prefix + b"AAAA")
        self._write("b.bin", prefix + b"BBBB")
        dup_payload = b"Q" * df.PARTIAL_BYTES + b"ZZZZ"
        self._write("d1.bin", dup_payload)
        self._write("d2.bin", dup_payload)
        entries = list(df.iter_files([self.root], [], min_size=1))
        dups = df.find_duplicates(df.group_by_size(entries))
        names_per_group = [{p.name for p in paths} for _, paths in dups]
        self.assertIn({"d1.bin", "d2.bin"}, names_per_group)
        for g in names_per_group:
            self.assertNotEqual(g, {"a.bin", "b.bin"})

    def test_find_duplicates_no_matches(self) -> None:
        self._write("a.txt", b"a")
        self._write("b.txt", b"bb")
        entries = list(df.iter_files([self.root], [], min_size=1))
        self.assertEqual(df.find_duplicates(df.group_by_size(entries)), [])

    def test_find_duplicates_reports_hash_progress(self) -> None:
        self._write("a.txt", b"same content")
        self._write("b.txt", b"same content")
        calls = []
        entries = list(df.iter_files([self.root], [], min_size=1))
        df.find_duplicates(
            df.group_by_size(entries),
            progress=lambda path, partial, ok, cached: calls.append(
                (path.name, partial, ok, cached)),
        )
        self.assertTrue(any(partial for _, partial, _, _ in calls))
        self.assertTrue(any(not partial for _, partial, _, _ in calls))
        self.assertTrue(all(ok for _, _, ok, _ in calls))

    # ---------- parse_action ----------
    def test_parse_action_default_enter(self) -> None:
        a = df.parse_action("", 3)
        self.assertEqual(a.kind, "execute")
        self.assertEqual(a.keep_index, 0)
        self.assertEqual(a.delete_indices, [1, 2])

    def test_parse_action_keep_specific(self) -> None:
        for line in ("k 2", "K 2", "k2"):
            a = df.parse_action(line, 4)
            self.assertEqual(a.kind, "execute", msg=line)
            self.assertEqual(a.keep_index, 1)
            self.assertEqual(a.delete_indices, [0, 2, 3])

    def test_parse_action_delete_specific(self) -> None:
        for line in ("d 1,3", "d 1 3", "d1,3"):
            a = df.parse_action(line, 4)
            self.assertEqual(a.kind, "execute", msg=line)
            self.assertEqual(sorted(a.delete_indices), [0, 2])

    def test_parse_action_skip_apply_quit(self) -> None:
        self.assertEqual(df.parse_action("s", 3).kind, "skip")
        self.assertEqual(df.parse_action("a", 3).kind, "apply_all")
        self.assertEqual(df.parse_action("q", 3).kind, "quit")

    def test_parse_action_invalid(self) -> None:
        self.assertIsNone(df.parse_action("xyz", 3))
        self.assertIsNone(df.parse_action("k", 3))         # no number
        self.assertIsNone(df.parse_action("k 9", 3))       # out of range
        self.assertIsNone(df.parse_action("d 1,2,3", 3))   # would delete all

    def test_parse_action_keep_index_zero_rejected(self) -> None:
        # 1-based input, so "0" is out of range.
        self.assertIsNone(df.parse_action("k 0", 3))

    # ---------- auto_decision ----------
    def test_auto_keep_newest(self) -> None:
        a = self._write("a.bin", b"x", mtime=1000)
        b = self._write("b.bin", b"x", mtime=2000)
        c = self._write("c.bin", b"x", mtime=1500)
        keep, deletes = df.auto_decision([a, b, c], "keep-newest", None)
        self.assertEqual(keep, 1)
        self.assertEqual(sorted(deletes), [0, 2])

    def test_auto_keep_oldest(self) -> None:
        a = self._write("a.bin", b"x", mtime=1000)
        b = self._write("b.bin", b"x", mtime=2000)
        keep, deletes = df.auto_decision([a, b], "keep-oldest", None)
        self.assertEqual(keep, 0)
        self.assertEqual(deletes, [1])

    def test_auto_keep_shortest(self) -> None:
        a = self._write("deeply/nested/long_name.bin", b"x")
        b = self._write("short.bin", b"x")
        keep, deletes = df.auto_decision([a, b], "keep-shortest", None)
        self.assertEqual(keep, 1)
        self.assertEqual(deletes, [0])

    def test_auto_keep_in_path(self) -> None:
        a = self._write("safe/a.bin", b"x")
        b = self._write("scratch/b.bin", b"x")
        keep, deletes = df.auto_decision([a, b], "keep-in", self.root / "safe")
        self.assertEqual(keep, 0)
        self.assertEqual(deletes, [1])

    # ---------- interactive flow ----------
    def test_interactive_basic_default(self) -> None:
        content = b"hello dup content"
        a = self._write("a.txt", content)
        b = self._write("b.txt", content)
        dups = df.find_duplicates(df.group_by_size(
            list(df.iter_files([self.root], [], min_size=1))))
        inputs = iter([""])  # accept default
        with patch("sys.stdout", StringIO()):
            decisions = df.build_decisions_interactive(dups, input_fn=lambda _: next(inputs))
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].keep_index, 0)
        self.assertEqual(decisions[0].delete_indices, [1])

    def test_interactive_apply_to_all(self) -> None:
        content1 = b"first dup content here"
        content2 = b"second dup content here!"
        self._write("g1_a.txt", content1)
        self._write("g1_b.txt", content1)
        self._write("g2_a.txt", content2)
        self._write("g2_b.txt", content2)
        dups = df.find_duplicates(df.group_by_size(
            list(df.iter_files([self.root], [], min_size=1))))
        self.assertEqual(len(dups), 2)
        inputs = iter(["a"])  # apply default to all on first prompt
        with patch("sys.stdout", StringIO()):
            decisions = df.build_decisions_interactive(dups, input_fn=lambda _: next(inputs))
        self.assertEqual(len(decisions), 2)
        for d in decisions:
            self.assertEqual(d.keep_index, 0)
            self.assertEqual(d.delete_indices, [1])

    def test_interactive_invalid_then_skip(self) -> None:
        content = b"another dup content"
        self._write("a.txt", content)
        self._write("b.txt", content)
        dups = df.find_duplicates(df.group_by_size(
            list(df.iter_files([self.root], [], min_size=1))))
        inputs = iter(["xyz", "s"])  # bad input, then skip
        with patch("sys.stdout", StringIO()):
            decisions = df.build_decisions_interactive(dups, input_fn=lambda _: next(inputs))
        self.assertEqual(decisions, [])

    def test_interactive_quit(self) -> None:
        content1 = b"quit test content one"
        content2 = b"quit test content two!"
        self._write("g1_a.txt", content1); self._write("g1_b.txt", content1)
        self._write("g2_a.txt", content2); self._write("g2_b.txt", content2)
        dups = df.find_duplicates(df.group_by_size(
            list(df.iter_files([self.root], [], min_size=1))))
        inputs = iter(["", "q"])  # process first, quit on second
        with patch("sys.stdout", StringIO()):
            decisions = df.build_decisions_interactive(dups, input_fn=lambda _: next(inputs))
        self.assertEqual(len(decisions), 1)

    # ---------- quarantine + restore round trip ----------
    def test_quarantine_and_restore_round_trip(self) -> None:
        content = b"round trip content"
        a = self._write("src/a.txt", content)
        b = self._write("src/copy/b.txt", content)
        self.assertTrue(a.exists() and b.exists())

        quar_root = self.root / "qroot"
        deletions = [(b, len(content))]
        count, freed, manifest, errors = df.quarantine_files(deletions, quar_root)
        self.assertEqual(count, 1)
        self.assertEqual(freed, len(content))
        self.assertEqual(errors, [])
        self.assertFalse(b.exists())
        self.assertTrue(a.exists())
        self.assertTrue((quar_root / "manifest.json").exists())

        rc = df.restore_from_manifest(quar_root)
        self.assertEqual(rc, 0)
        self.assertTrue(b.exists())
        self.assertEqual(b.read_bytes(), content)

    def test_quarantine_handles_dest_collision(self) -> None:
        # Two files with the same relative path under different roots could
        # collide in quarantine. Simulate by pre-creating destination.
        quar_root = self.root / "qroot"
        f = self._write("hello.txt", b"abc")
        # Pre-create what would be the destination so quarantine bumps the name.
        rel = df._quarantine_relpath(f)
        dest_dir = quar_root / rel.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        (quar_root / rel).write_bytes(b"squatter")
        count, _, manifest, errors = df.quarantine_files([(f, 3)], quar_root)
        self.assertEqual(count, 1)
        self.assertEqual(errors, [])
        self.assertNotEqual(Path(manifest[0]["quarantined"]).name, "hello.txt")

    def test_restore_missing_manifest(self) -> None:
        with patch("sys.stderr", StringIO()):
            rc = df.restore_from_manifest(self.root / "nope.json")
        self.assertEqual(rc, 2)

    def test_restore_skips_when_original_exists(self) -> None:
        content = b"restore conflict content"
        a = self._write("a.txt", content)
        b = self._write("b.txt", content)
        quar_root = self.root / "qroot"
        df.quarantine_files([(b, len(content))], quar_root)
        # Re-create something at b's original path so restore must skip.
        b.write_bytes(b"new content")
        buf = StringIO()
        with patch("sys.stderr", buf), patch("sys.stdout", StringIO()):
            rc = df.restore_from_manifest(quar_root)
        self.assertEqual(rc, 0)
        self.assertIn("occupied", buf.getvalue())
        self.assertEqual(b.read_bytes(), b"new content")

    # ---------- permanent delete ----------
    def test_permanent_delete(self) -> None:
        a = self._write("a.txt", b"abc")
        b = self._write("b.txt", b"def")
        count, freed, errors = df.permanent_delete_files([(a, 3), (b, 3)])
        self.assertEqual(count, 2)
        self.assertEqual(freed, 6)
        self.assertEqual(errors, [])
        self.assertFalse(a.exists())
        self.assertFalse(b.exists())

    def test_path_is_under_any(self) -> None:
        protected = self.root / "protected"
        protected.mkdir()
        inside = self._write("protected/a.txt", b"x")
        outside = self._write("outside.txt", b"x")
        self.assertTrue(df.path_is_under_any(inside, [protected]))
        self.assertFalse(df.path_is_under_any(outside, [protected]))

    def test_validate_deletion_candidates_skips_protected(self) -> None:
        protected = self.root / "protected"
        protected.mkdir()
        f = self._write("protected/a.txt", b"abc")
        candidates = [
            df.DeletionCandidate(
                path=f,
                size=3,
                mtime_ts=f.stat().st_mtime,
                sha256=df.hash_file(f, partial=False),
            )
        ]
        ready, errors = df.validate_deletion_candidates(
            candidates, protected_roots=[protected],
        )
        self.assertEqual(ready, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("protected", errors[0])

    def test_validate_deletion_candidates_detects_content_change(self) -> None:
        f = self._write("same_size.txt", b"abc")
        candidates = [
            df.DeletionCandidate(
                path=f,
                size=3,
                mtime_ts=f.stat().st_mtime,
                sha256=df.hash_file(f, partial=False),
            )
        ]
        f.write_bytes(b"xyz")
        ready, errors = df.validate_deletion_candidates(candidates)
        self.assertEqual(ready, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("content changed", errors[0])

    def test_validate_deletion_candidates_accepts_unchanged(self) -> None:
        f = self._write("ok.txt", b"abc")
        candidates = [
            df.DeletionCandidate(
                path=f,
                size=3,
                mtime_ts=f.stat().st_mtime,
                sha256=df.hash_file(f, partial=False),
            )
        ]
        ready, errors = df.validate_deletion_candidates(candidates)
        self.assertEqual(ready, [(f, 3)])
        self.assertEqual(errors, [])

    def test_list_quarantine_runs(self) -> None:
        f = self._write("dup.txt", b"abc")
        qbase = self.root / df.QUARANTINE_DIRNAME
        qrun = qbase / "run-20260101-010101"
        df.quarantine_files([(f, 3)], qrun)
        runs = df.list_quarantine_runs(qbase)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["file_count"], 1)
        self.assertEqual(runs[0]["available_count"], 1)
        self.assertEqual(runs[0]["total_bytes"], 3)

    # ---------- report formats ----------
    def test_report_as_json_shape(self) -> None:
        content = b"hello dup"
        self._write("x.txt", content)
        self._write("y/x.txt", content)
        entries = list(df.iter_files([self.root], [], min_size=1))
        dups = df.find_duplicates(df.group_by_size(entries))
        payload = json.loads(df.report_as_json(dups))
        self.assertEqual(len(payload["groups"]), 1)
        self.assertEqual(payload["groups"][0]["size_bytes"], len(content))
        self.assertEqual(payload["total_wasted_bytes"], len(content))

    def test_print_report_empty(self) -> None:
        buf = StringIO()
        with patch("sys.stdout", buf):
            df.print_report([])
        self.assertIn("No duplicates", buf.getvalue())

    # ---------- preview ----------
    def test_preview_text_file(self) -> None:
        p = self._write("note.txt", b"hello preview")
        preview = df.preview_file(p)
        self.assertEqual(preview["kind"], "text")
        self.assertIn("hello preview", preview["text"])
        self.assertEqual(preview["mime"], "text/plain")

    def test_preview_binary_metadata(self) -> None:
        p = self._write("blob.bin", b"\x00\x01\x02")
        preview = df.preview_file(p)
        self.assertEqual(preview["kind"], "metadata")
        self.assertEqual(preview["size"], 3)

    def test_preview_png_image(self) -> None:
        # Minimal PNG signature plus IHDR-ish bytes is enough for the preview
        # path because the UI only needs a bounded data URL.
        p = self._write("tiny.png", b"\x89PNG\r\n\x1a\n" + b"x" * 16)
        preview = df.preview_file(p)
        self.assertEqual(preview["kind"], "image")
        self.assertTrue(preview["data_url"].startswith("data:image/png;base64,"))

    def test_preview_missing_file(self) -> None:
        preview = df.preview_file(self.root / "missing.txt")
        self.assertEqual(preview["kind"], "error")

    # ---------- summarize_plan ----------
    def test_summarize_plan(self) -> None:
        a = self._write("a.txt", b"xx"); b = self._write("b.txt", b"xx")
        c = self._write("c.txt", b"yyy"); d = self._write("d.txt", b"yyy"); e = self._write("e.txt", b"yyy")
        decisions = [
            df.Decision(size=2, paths=[a, b], keep_index=0, delete_indices=[1]),
            df.Decision(size=3, paths=[c, d, e], keep_index=0, delete_indices=[1, 2]),
        ]
        n, b_freed = df.summarize_plan(decisions)
        self.assertEqual(n, 3)
        self.assertEqual(b_freed, 2 * 1 + 3 * 2)

    # ---------- main() smoke ----------
    def test_main_smoke(self) -> None:
        content = b"smoke test content"
        self._write("one.bin", content)
        self._write("two.bin", content)
        buf_out, buf_err = StringIO(), StringIO()
        with patch("sys.stdout", buf_out), patch("sys.stderr", buf_err):
            rc = df.main([str(self.root), "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf_out.getvalue())
        self.assertEqual(len(payload["groups"]), 1)
        self.assertEqual(payload["groups"][0]["size_bytes"], len(content))

    def test_main_rejects_non_directory(self) -> None:
        f = self._write("not_a_dir.txt", b"x")
        with patch("sys.stderr", StringIO()):
            rc = df.main([str(f)])
        self.assertEqual(rc, 2)

    def test_main_auto_keep_newest_quarantines(self) -> None:
        content = b"auto policy test content"
        a = self._write("a.txt", content, mtime=1000)
        b = self._write("b.txt", content, mtime=2000)
        # Run main from inside our tmp dir so the quarantine lands here.
        cwd0 = os.getcwd()
        try:
            os.chdir(self.root)
            df.reset_app_data_dir_cache()  # re-resolve to the new cwd
            with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
                rc = df.main([str(self.root), "--auto", "keep-newest", "--yes"])
            self.assertEqual(rc, 0)
            self.assertTrue(b.exists())     # newer kept
            self.assertFalse(a.exists())    # older quarantined
            qroots = list((self.root / df.QUARANTINE_DIRNAME).iterdir())
            self.assertEqual(len(qroots), 1)
            self.assertTrue((qroots[0] / "manifest.json").exists())
        finally:
            os.chdir(cwd0)


    # ---------- empty folder finder ----------
    def test_find_empty_basic(self) -> None:
        (self.root / "empty_leaf").mkdir()
        (self.root / "has_file").mkdir()
        self._write("has_file/x.txt", b"x")
        empties = df.find_empty_folders([self.root], [])
        names = [p.name for p in empties]
        self.assertIn("empty_leaf", names)
        self.assertNotIn("has_file", names)

    def test_find_empty_recursive(self) -> None:
        (self.root / "a" / "b" / "c").mkdir(parents=True)
        empties = df.find_empty_folders([self.root], [])
        # All three nested folders should be reported, deepest first.
        names = [p.name for p in empties]
        self.assertEqual(names, ["c", "b", "a"])

    def test_find_empty_not_empty_if_subdir_has_file(self) -> None:
        (self.root / "outer" / "inner").mkdir(parents=True)
        self._write("outer/inner/file.txt", b"x")
        empties = df.find_empty_folders([self.root], [])
        self.assertEqual(empties, [])

    def test_find_empty_ignore_junk(self) -> None:
        d = self.root / "junky"
        d.mkdir()
        self._write("junky/Thumbs.db", b"thumbnail")
        self._write("junky/.DS_Store", b"mac junk")
        empties_with_junk_ignored = df.find_empty_folders(
            [self.root], [], ignore_junk=True)
        empties_keeping_junk = df.find_empty_folders(
            [self.root], [], ignore_junk=False)
        self.assertEqual([p.name for p in empties_with_junk_ignored], ["junky"])
        self.assertEqual(empties_keeping_junk, [])

    def test_find_empty_excludes_root_itself(self) -> None:
        # The user-picked root is never reported as a deletable empty folder.
        # Even with literally nothing inside, it shouldn't appear.
        inner = self.root / "inside"
        inner.mkdir()
        empties = df.find_empty_folders([self.root], [])
        self.assertNotIn(self.root.resolve(), [p.resolve() for p in empties])

    def test_find_empty_respects_skip(self) -> None:
        (self.root / "keep_empty").mkdir()
        (self.root / "node_modules").mkdir()
        empties = df.find_empty_folders([self.root], ["node_modules"])
        names = [p.name for p in empties]
        self.assertIn("keep_empty", names)
        self.assertNotIn("node_modules", names)

    def test_delete_empty_folders_basic(self) -> None:
        d = self.root / "empty1"
        d.mkdir()
        count, errors = df.delete_empty_folders([d])
        self.assertEqual(count, 1)
        self.assertEqual(errors, [])
        self.assertFalse(d.exists())

    def test_delete_empty_folders_removes_junk_first(self) -> None:
        d = self.root / "junky"
        d.mkdir()
        thumbs = self._write("junky/Thumbs.db", b"x")
        count, errors = df.delete_empty_folders([d])
        self.assertEqual(count, 1)
        self.assertEqual(errors, [])
        self.assertFalse(d.exists())
        self.assertFalse(thumbs.exists())

    def test_delete_empty_folders_fails_safely_if_not_empty(self) -> None:
        d = self.root / "secretly_full"
        d.mkdir()
        real = self._write("secretly_full/important.txt", b"data")
        count, errors = df.delete_empty_folders([d])
        self.assertEqual(count, 0)
        self.assertEqual(len(errors), 1)
        # rmdir should refuse; the real file must still be there.
        self.assertTrue(d.exists())
        self.assertTrue(real.exists())

    def test_delete_empty_folders_orders_deepest_first(self) -> None:
        (self.root / "a" / "b" / "c").mkdir(parents=True)
        empties = df.find_empty_folders([self.root], [])
        count, errors = df.delete_empty_folders(empties)
        self.assertEqual(count, 3)
        self.assertEqual(errors, [])
        self.assertFalse((self.root / "a").exists())

    # ---------- cancellation ----------
    def test_iter_files_cancellation(self) -> None:
        import threading
        self._write("a.txt", b"x")
        self._write("b.txt", b"x")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(df.ScanCancelled):
            list(df.iter_files([self.root], [], min_size=1, cancel=cancel))

    def test_find_duplicates_cancellation(self) -> None:
        import threading
        # Build several same-size pairs so there's hashing work to interrupt.
        for i in range(8):
            self._write(f"a{i}.bin", b"X" * 1024)
            self._write(f"b{i}.bin", b"X" * 1024)
        entries = list(df.iter_files([self.root], [], min_size=1))
        size_groups = df.group_by_size(entries)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(df.ScanCancelled):
            df.find_duplicates(size_groups, cancel=cancel)

    def test_find_empty_folders_cancellation(self) -> None:
        import threading
        for i in range(5):
            (self.root / f"empty{i}").mkdir()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(df.ScanCancelled):
            df.find_empty_folders([self.root], [], cancel=cancel)

    def test_cancel_unset_runs_normally(self) -> None:
        import threading
        self._write("a.txt", b"hello")
        self._write("b.txt", b"hello")
        cancel = threading.Event()  # not set
        entries = list(df.iter_files([self.root], [], min_size=1, cancel=cancel))
        self.assertEqual(len(entries), 2)
        dups = df.find_duplicates(df.group_by_size(entries), cancel=cancel)
        self.assertEqual(len(dups), 1)

    def test_hash_file_cancellation(self) -> None:
        """Cancellation should fire mid-hash on a large file, not only between files."""
        import threading
        # File large enough that the chunked read loop runs multiple iterations.
        big = self._write("big.bin", b"X" * (df.CHUNK * 4))
        cancel = threading.Event()
        cancel.set()  # pre-set: first chunk-loop iteration should bail
        with self.assertRaises(df.ScanCancelled):
            df.hash_file(big, partial=False, cancel=cancel)

    # ---------- data dir resolution ----------
    def test_resolve_data_dir_picks_first_writable(self) -> None:
        a = self.root / "a"
        b = self.root / "b"
        result = df._resolve_data_dir([a, b])
        self.assertEqual(result, a)
        self.assertTrue(a.is_dir())

    def test_resolve_data_dir_skips_blocked_candidate(self) -> None:
        # A pre-existing FILE (not a dir) at the candidate path blocks mkdir.
        blocker = self._write("blocker", b"in the way")
        fallback = self.root / "fallback"
        result = df._resolve_data_dir([blocker, fallback])
        self.assertEqual(result, fallback)
        self.assertTrue(fallback.is_dir())

    def test_resolve_data_dir_raises_when_all_fail(self) -> None:
        b1 = self._write("b1", b"x")
        b2 = self._write("b2", b"x")
        with self.assertRaises(OSError):
            df._resolve_data_dir([b1, b2])

    def test_app_data_dir_is_cached(self) -> None:
        df.reset_app_data_dir_cache()
        try:
            d1 = df.app_data_dir()
            d2 = df.app_data_dir()
            self.assertEqual(d1, d2)
            self.assertTrue(d1.is_dir())
        finally:
            df.reset_app_data_dir_cache()

    # ---------- largest items ----------
    def test_compute_top_largest_orders_by_size(self) -> None:
        self._write("small.txt", b"a")
        self._write("medium.txt", b"a" * 100)
        self._write("big.txt", b"a" * 10000)
        files, folders, total_files, total_bytes = df.compute_top_largest(
            [self.root], [], min_size=1, top_files=10, top_folders=10,
        )
        self.assertEqual([f.path.name for f in files],
                         ["big.txt", "medium.txt", "small.txt"])
        self.assertEqual(total_files, 3)
        self.assertEqual(total_bytes, 1 + 100 + 10000)

    def test_compute_top_largest_min_size(self) -> None:
        self._write("tiny.txt", b"x")
        self._write("real.txt", b"x" * 100)
        files, _, total_files, _ = df.compute_top_largest(
            [self.root], [], min_size=50, top_files=10, top_folders=10,
        )
        self.assertEqual([f.path.name for f in files], ["real.txt"])
        self.assertEqual(total_files, 1)

    def test_compute_top_largest_caps_top_n(self) -> None:
        for i in range(20):
            self._write(f"f{i:02d}.bin", b"x" * (10 * (i + 1)))
        files, _, _, _ = df.compute_top_largest(
            [self.root], [], min_size=1, top_files=5, top_folders=10,
        )
        self.assertEqual(len(files), 5)
        # Largest first; the heaviest 5 are i=15..19.
        sizes = [f.size for f in files]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(files[0].path.name, "f19.bin")

    def test_compute_top_largest_folder_recursive_sum(self) -> None:
        self._write("a/b/c/file.bin", b"x" * 1000)
        self._write("a/b/sibling.bin", b"x" * 500)
        self._write("loose.bin", b"x" * 50)
        _, folders, _, _ = df.compute_top_largest(
            [self.root], [], min_size=1, top_files=10, top_folders=20,
        )
        by_name = {f.path.name: f for f in folders}
        # root (tempdir name) holds everything = 1550
        # 'a' holds 1500, 'b' holds 1500, 'c' holds 1000
        self.assertEqual(by_name["a"].total_size, 1500)
        self.assertEqual(by_name["b"].total_size, 1500)
        self.assertEqual(by_name["c"].total_size, 1000)
        self.assertEqual(by_name["a"].file_count, 2)
        self.assertEqual(by_name["c"].file_count, 1)

    def test_compute_top_largest_intermediate_dirs_with_no_direct_files(self) -> None:
        # 'a' has only subdir 'b'; 'b' has the file. 'a' should still appear
        # with the correct recursive size.
        self._write("a/b/file.bin", b"y" * 250)
        _, folders, _, _ = df.compute_top_largest(
            [self.root], [], min_size=1, top_folders=10,
        )
        by_name = {f.path.name: f for f in folders}
        self.assertEqual(by_name["a"].total_size, 250)
        self.assertEqual(by_name["a"].direct_size, 0)
        self.assertEqual(by_name["a"].file_count, 1)

    def test_compute_top_largest_respects_skip(self) -> None:
        self._write("keep.bin", b"x" * 100)
        self._write("node_modules/big.bin", b"x" * 99999)
        files, _, total_files, _ = df.compute_top_largest(
            [self.root], ["node_modules"], min_size=1,
        )
        names = [f.path.name for f in files]
        self.assertIn("keep.bin", names)
        self.assertNotIn("big.bin", names)
        self.assertEqual(total_files, 1)

    def test_compute_top_largest_cancellation(self) -> None:
        import threading
        for i in range(10):
            self._write(f"f{i}.bin", b"x" * 100)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(df.ScanCancelled):
            df.compute_top_largest([self.root], [], cancel=cancel)

    def test_main_largest_cli_json(self) -> None:
        self._write("a.bin", b"x" * 1000)
        self._write("b.bin", b"x" * 100)
        buf_out, buf_err = StringIO(), StringIO()
        with patch("sys.stdout", buf_out), patch("sys.stderr", buf_err):
            rc = df.main([str(self.root), "--largest", "--json", "--top-files", "5"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf_out.getvalue())
        self.assertEqual(payload["total_files"], 2)
        self.assertEqual(payload["total_bytes"], 1100)
        self.assertEqual([f["path"].endswith("a.bin") for f in payload["files"]][0], True)

    # ---------- similar images ----------
    def _make_patterned_jpeg(self, rel: str, *, pattern: str,
                             quality: int = 90, brightness: int = 0) -> Path:
        """Create a 128x128 JPEG with deterministic structure that survives
        the 9x8 dHash downsample. `pattern` ∈ {'diag','vert','horiz'}."""
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        img = Image.new("RGB", (128, 128))
        for y in range(128):
            for x in range(128):
                if pattern == "diag":
                    v = ((x + y) // 8) * 25 % 255
                elif pattern == "vert":
                    v = (y // 8) * 25 % 255
                elif pattern == "horiz":
                    v = (x // 8) * 25 % 255
                else:
                    v = 128
                v = min(255, max(0, v + brightness))
                img.putpixel((x, y), (v, v, v))
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        img.save(p, format="JPEG", quality=quality)
        return p

    def test_dhash_identical_content_matches(self) -> None:
        if not df.pillow_available():
            self.skipTest("Pillow not installed")
        p1 = self._make_patterned_jpeg("a.jpg", pattern="diag")
        p2 = self._make_patterned_jpeg("b.jpg", pattern="diag")
        self.assertEqual(
            df.hamming_distance(df.compute_image_dhash(p1),
                                df.compute_image_dhash(p2)),
            0,
        )

    def test_dhash_near_duplicate_low_distance(self) -> None:
        if not df.pillow_available():
            self.skipTest("Pillow not installed")
        # Same content, different JPEG quality + brightness shift.
        p1 = self._make_patterned_jpeg("q90.jpg", pattern="diag", quality=90)
        p2 = self._make_patterned_jpeg("q50.jpg", pattern="diag", quality=50)
        p3 = self._make_patterned_jpeg("b10.jpg", pattern="diag", brightness=10)
        d12 = df.hamming_distance(df.compute_image_dhash(p1),
                                  df.compute_image_dhash(p2))
        d13 = df.hamming_distance(df.compute_image_dhash(p1),
                                  df.compute_image_dhash(p3))
        self.assertLessEqual(d12, df.DEFAULT_SIMILARITY_THRESHOLD)
        self.assertLessEqual(d13, df.DEFAULT_SIMILARITY_THRESHOLD)

    def test_dhash_different_patterns_high_distance(self) -> None:
        if not df.pillow_available():
            self.skipTest("Pillow not installed")
        p1 = self._make_patterned_jpeg("d.jpg", pattern="diag")
        p2 = self._make_patterned_jpeg("h.jpg", pattern="horiz")
        d = df.hamming_distance(df.compute_image_dhash(p1),
                                df.compute_image_dhash(p2))
        self.assertGreaterEqual(d, 15)

    def test_find_similar_images_groups_correctly(self) -> None:
        if not df.pillow_available():
            self.skipTest("Pillow not installed")
        # Group A: 3 variants of the diag pattern (near-identical hashes)
        self._make_patterned_jpeg("a1.jpg", pattern="diag", quality=90)
        self._make_patterned_jpeg("a2.jpg", pattern="diag", quality=60)
        self._make_patterned_jpeg("a3.jpg", pattern="diag", brightness=8)
        # Group B: 2 variants of the vert pattern
        self._make_patterned_jpeg("b1.jpg", pattern="vert", quality=90)
        self._make_patterned_jpeg("b2.jpg", pattern="vert", quality=60)
        # Unique: horiz pattern
        self._make_patterned_jpeg("c.jpg", pattern="horiz")

        groups, scanned, _skipped = df.find_similar_images([self.root], [])
        self.assertEqual(scanned, 6)
        names_per_group = [sorted(im.path.name for im in g) for g in groups]
        self.assertEqual(len(groups), 2)
        all_grouped = {n for grp in names_per_group for n in grp}
        self.assertNotIn("c.jpg", all_grouped)
        self.assertIn({"a1.jpg", "a2.jpg", "a3.jpg"},
                      [set(g) for g in names_per_group])
        self.assertIn({"b1.jpg", "b2.jpg"},
                      [set(g) for g in names_per_group])

    def test_find_similar_images_returns_thumbnail(self) -> None:
        if not df.pillow_available():
            self.skipTest("Pillow not installed")
        self._make_patterned_jpeg("p1.jpg", pattern="diag")
        self._make_patterned_jpeg("p2.jpg", pattern="diag", quality=70)
        groups, _, _ = df.find_similar_images([self.root], [])
        self.assertTrue(groups, "expected at least one similar group")
        for g in groups:
            for im in g:
                self.assertIsNotNone(im.thumbnail)
                self.assertTrue(im.thumbnail.startswith("data:image/jpeg;base64,"))

    def test_find_similar_images_cancellation(self) -> None:
        if not df.pillow_available():
            self.skipTest("Pillow not installed")
        import threading
        for i in range(4):
            self._make_patterned_jpeg(f"p{i}.jpg", pattern="diag",
                                      brightness=i * 5)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(df.ScanCancelled):
            df.find_similar_images([self.root], [], cancel=cancel)

    def test_find_similar_images_returns_empty_without_pillow(self) -> None:
        # Force the "no Pillow" code path by patching the cached flag.
        original = getattr(df.pillow_available, "_cached", None)
        df.pillow_available._cached = False
        try:
            groups, scanned, skipped = df.find_similar_images([self.root], [])
            self.assertEqual(groups, [])
            self.assertEqual(scanned, 0)
            self.assertEqual(skipped, 0)
        finally:
            if original is not None:
                df.pillow_available._cached = original
            else:
                if hasattr(df.pillow_available, "_cached"):
                    del df.pillow_available._cached

    def test_hash_file_partial_not_cancellable_mid_read(self) -> None:
        """Partial hash is one short read; no cancel point inside is OK."""
        import threading
        p = self._write("small.bin", b"x" * 100)
        cancel = threading.Event()  # not set
        # Should succeed normally.
        self.assertIsNotNone(df.hash_file(p, partial=True, cancel=cancel))

    def test_main_find_empty_cli(self) -> None:
        (self.root / "empty_one").mkdir()
        (self.root / "with_data").mkdir()
        self._write("with_data/file.txt", b"x")
        buf_out, buf_err = StringIO(), StringIO()
        with patch("sys.stdout", buf_out), patch("sys.stderr", buf_err):
            rc = df.main([str(self.root), "--find-empty", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf_out.getvalue())
        # The root has 2 subdirs; only "empty_one" should be reported.
        # (with_data is non-empty because it contains a file)
        names = [Path(p).name for p in payload["empty_folders"]]
        self.assertIn("empty_one", names)
        self.assertNotIn("with_data", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
