"""The patch applier must agree with `git apply` byte for byte.

    python -m unittest discover tests            (needs git on PATH)

Uses hand-written edge cases, plus every sample patch when a pinned sqlglot
checkout is given in SQLGLOT_CHECKOUT.
"""

import glob
import os
import subprocess
import tempfile
import unittest

from blast import patch as patching

HERE = os.path.dirname(os.path.abspath(__file__))


def git_apply(files: dict, diff: str) -> dict:
    with tempfile.TemporaryDirectory() as directory:
        for rel, text in files.items():
            path = os.path.join(directory, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", newline="") as handle:
                handle.write(text)
        subprocess.run(["git", "init", "-q"], cwd=directory, check=True)
        subprocess.run(["git", "apply", "-"], cwd=directory, input=diff.encode(), check=True)
        result = {}
        for root, dirs, names in os.walk(directory):
            dirs[:] = [d for d in dirs if d != ".git"]
            for name in names:
                path = os.path.join(root, name)
                with open(path, newline="") as handle:
                    result[os.path.relpath(path, directory)] = handle.read()
        return result


def make_diff(before: dict, after: dict) -> str:
    with tempfile.TemporaryDirectory() as directory:
        for side, files in (("a", before), ("b", after)):
            for rel, text in files.items():
                path = os.path.join(directory, side, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", newline="") as handle:
                    handle.write(text)
            os.makedirs(os.path.join(directory, side), exist_ok=True)
        done = subprocess.run(
            ["git", "diff", "--no-index", "--no-color", "a", "b"], cwd=directory, capture_output=True
        )
        return done.stdout.decode()


def ours(files: dict, diff: str) -> dict:
    result = dict(files)
    for change in patching.parse(diff):
        old = files.get(change.old_path) if change.old_path else None
        new = patching.apply(change, old)
        if change.old_path and change.old_path != change.new_path:
            result.pop(change.old_path, None)
        if change.new_path is None:
            result.pop(change.old_path, None)
        else:
            result[change.new_path] = new
    return result


BASE = "".join(f"line {i}\n" for i in range(1, 41))
CASES = {
    "modify": ({"x.py": BASE}, {"x.py": BASE.replace("line 20\n", "LINE 20\n")}),
    "insert": ({"x.py": BASE}, {"x.py": BASE.replace("line 20\n", "line 20\nnew\nnewer\n")}),
    "delete": ({"x.py": BASE}, {"x.py": BASE.replace("line 20\nline 21\n", "")}),
    "two hunks": ({"x.py": BASE}, {"x.py": BASE.replace("line 3\n", "three\n").replace("line 35\n", "a\nb\n")}),
    "first line": ({"x.py": BASE}, {"x.py": "top\n" + BASE}),
    "last line": ({"x.py": BASE}, {"x.py": BASE + "end\n"}),
    "drop final newline": ({"x.py": BASE}, {"x.py": BASE[:-1]}),
    "add final newline": ({"x.py": BASE[:-1]}, {"x.py": BASE}),
    "no newline both": ({"x.py": BASE[:-1]}, {"x.py": BASE[:-1].replace("line 40", "forty")}),
    "new file": ({"x.py": BASE}, {"x.py": BASE, "d/new.py": "a = 1\nb = 2\n"}),
    "delete file": ({"x.py": BASE, "y.py": "gone\n"}, {"x.py": BASE}),
    "blank lines": ({"x.py": "a\n\n\nb\n\n\nc\n"}, {"x.py": "a\n\n\nB\n\n\n\nc\n"}),
    "crlf": ({"x.py": "a\r\nb\r\nc\r\n"}, {"x.py": "a\r\nB\r\nc\r\n"}),
    "two files": ({"x.py": BASE, "y.sql": "SELECT 1;\n"}, {"x.py": BASE.replace("line 9\n", ""), "y.sql": "SELECT 2;\n"}),
}


class TestPatch(unittest.TestCase):
    def test_cases_match_git(self):
        for name, (before, after) in CASES.items():
            with self.subTest(name):
                diff = make_diff(before, after)
                diff = diff.replace("a/a/", "a/").replace("b/b/", "b/")
                self.assertEqual(ours(before, diff), after)
                self.assertEqual(git_apply(before, diff), after)

    def test_rename(self):
        diff = (
            "diff --git a/x.py b/z.py\nsimilarity index 90%\nrename from x.py\nrename to z.py\n"
            "--- a/x.py\n+++ b/z.py\n@@ -1,3 +1,3 @@\n line 1\n-line 2\n+two\n line 3\n"
        )
        self.assertEqual(ours({"x.py": BASE}, diff), {"z.py": BASE.replace("line 2\n", "two\n")})

    def test_rejects_what_does_not_apply(self):
        diff = make_diff({"x.py": BASE}, {"x.py": BASE.replace("line 20\n", "X\n")}).replace("a/a/", "a/").replace("b/b/", "b/")
        with self.assertRaises(patching.PatchError):
            ours({"x.py": BASE.replace("line 19\n", "different\n")}, diff)
        with self.assertRaises(patching.PatchError):
            truncated = patching.parse("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n a\n")
            patching.apply(truncated[0], "a\nb\n")
        with self.assertRaises(patching.PatchError):
            patching.parse("diff --git a/../x b/../x\n--- a/../x\n+++ b/../x\n@@ -1 +1 @@\n-a\n+b\n")

    def test_blocks(self):
        diff = make_diff({"x.py": BASE}, {"x.py": BASE.replace("line 20\n", "line 20\nnew\n").replace("line 5\n", "five\n")})
        change = patching.parse(diff.replace("a/a/", "a/").replace("b/b/", "b/"))[0]
        patching.apply(change, BASE)
        self.assertEqual([(b.start, b.old, b.new) for b in change.blocks], [(5, ["line 5"], ["five"]), (21, [], ["new"])])

    @unittest.skipUnless(os.environ.get("SQLGLOT_CHECKOUT"), "set SQLGLOT_CHECKOUT to the pinned sqlglot checkout")
    def test_sample_patches_match_git(self):
        checkout = os.environ["SQLGLOT_CHECKOUT"]
        patches = sorted(glob.glob(os.path.join(HERE, "..", "sample", "commits", "*.patch")))
        self.assertTrue(patches)
        for path in patches:
            with self.subTest(os.path.basename(path)):
                with open(path, newline="") as handle:
                    diff = handle.read()
                before = {}
                for change in patching.parse(diff):
                    if change.old_path:
                        with open(os.path.join(checkout, change.old_path), newline="") as handle:
                            before[change.old_path] = handle.read()
                self.assertEqual(ours(before, diff), git_apply(before, diff))


if __name__ == "__main__":
    unittest.main()
