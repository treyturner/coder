"""Check official metadata selection and fail-closed draft derivation."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bump", ROOT / "scripts/bump.py")
bump = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bump)


class BumpTests(unittest.TestCase):
    def test_go_selection_is_checked_and_limited_to_the_requested_series(self):
        def release(version):
            return {"version": version, "stable": True, "files": [{"os": "linux", "arch": "amd64", "kind": "archive", "filename": version + ".linux-amd64.tar.gz", "sha256": "a" * 64}]}
        values = [release("go1.26.5"), release("go1.26.7"), release("go1.27.1")]
        self.assertEqual(bump.checked_go("go1.26", values)["version"], "go1.26.7")
        self.assertEqual(bump.checked_go("go1.26.5", values)["version"], "go1.26.5")
        values[0]["files"][0]["sha256"] = "untrusted"
        with self.assertRaisesRegex(bump.BumpError, "checksum"):
            bump.checked_go("go1.26.5", values)

    def test_replacement_requires_the_official_fork_and_pseudo_version(self):
        value = {"Replace": [{"Old": {"Path": "tailscale.com"}, "New": {"Path": "github.com/coder/tailscale", "Version": "v1.1.1-0.20260529105257-b7c5fc6e6399"}}]}
        self.assertEqual(bump.replacement(value)[1], "b7c5fc6e6399")
        value["Replace"][0]["New"]["Path"] = "github.com/another/tailscale"
        with self.assertRaisesRegex(bump.BumpError, "manual review"):
            bump.replacement(value)

    def test_rollback_uses_only_the_exact_versioned_archive(self):
        content = ("a" * 64 + "  coder_2.37.5_linux_amd64.tar.gz\n" + "b" * 64 + "  coder_2.37.5_linux_arm64.tar.gz\n").encode()
        self.assertEqual(bump.rollback("2.37.5", content)["sha256"], "a" * 64)
        with self.assertRaisesRegex(bump.BumpError, "ambiguous"):
            bump.rollback("2.37.5", content + content)

    def test_invalid_tags_or_reviewed_output_never_fetch_source(self):
        for tag, path in (("../main", ROOT / "sources.next.json"), ("v2.37.4", ROOT / "sources.lock.json")):
            with self.subTest(tag=tag), self.assertRaises(bump.BumpError):
                bump.draft(tag, ROOT, path)

    def test_effective_module_replacements_require_manual_review(self):
        expected = ["example.test/module"]
        item = {"Path": expected[0], "Version": "v1.2.3"}
        self.assertEqual(bump.effective_modules(json.dumps(item).encode(), expected), {expected[0]: "v1.2.3"})
        for replacement in ({"Path": "example.test/fork", "Version": "v1.2.4"}, {"Path": "../local"}):
            changed = dict(item, Replace=replacement)
            with self.subTest(replacement=replacement), self.assertRaisesRegex(bump.BumpError, "manual review"):
                bump.effective_modules(json.dumps(changed).encode(), expected)
        for value in (b"", json.dumps(item).encode() * 2):
            with self.assertRaisesRegex(bump.BumpError, "manual review"):
                bump.effective_modules(value, expected)

    def test_late_output_collision_preserves_the_other_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "next.json"
            bump.write_draft(output, {"review": "existing"})
            before = output.read_bytes()
            with self.assertRaisesRegex(bump.BumpError, "already exists"):
                bump.write_draft(output, {"review": "replacement"})
            self.assertEqual(output.read_bytes(), before)
