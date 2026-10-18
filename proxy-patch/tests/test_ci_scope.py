"""Check that fork packaging CI cannot suppress production-source checks."""
import importlib.util
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ci_scope", ROOT / "scripts/ci_scope.py")
ci_scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_scope)
BASE = "1" * 40


class ScopeTests(unittest.TestCase):
    def classify(self, paths):
        with patch.object(ci_scope, "git", side_effect=(b"", b"\0".join(path.encode() for path in paths) + b"\0")):
            return ci_scope.wrapper_only(ROOT, "https://github.com", "treyturner/coder", "pull_request", BASE)

    def test_production_paths_always_keep_ordinary_ci(self):
        allowed = ["proxy-patch/scripts/pipeline.py", ".github/workflows/proxy-patch-verify.yml"]
        self.assertTrue(self.classify(allowed))
        for path in (".github/workflows/ci.yaml", "go.mod", "cli/ssh.go", "coderd/database/queries/users.sql", "site/src/App.tsx", ".github/workflows/release.yaml", "proxy-patch-other/file"):
            with self.subTest(path=path):
                self.assertFalse(self.classify([*allowed, path]))
        self.assertFalse(self.classify([]))
        # --no-renames keeps the original production path when moved into the queue.
        self.assertFalse(self.classify(["cli/ssh.go", "proxy-patch/ssh.go"]))

    def test_other_repositories_origins_and_events_never_skip(self):
        for server, repo, event in (("https://github.com", "coder/coder", "push"), ("https://forgejo.treyturner.info", "treyturner/coder", "push"), ("https://github.com", "treyturner/coder", "workflow_dispatch")):
            with self.subTest(server=server, repo=repo, event=event), patch.object(ci_scope, "git") as command:
                self.assertFalse(ci_scope.wrapper_only(ROOT, server, repo, event, BASE))
                command.assert_not_called()

    def test_missing_history_is_fetched_using_a_validated_revision(self):
        with patch.object(ci_scope, "git", side_effect=(None, b"", b"proxy-patch/README.md\0")) as command:
            self.assertTrue(ci_scope.wrapper_only(ROOT, "https://github.com", "treyturner/coder", "push", BASE, "refs/heads/main"))
            self.assertEqual(command.call_args_list[1].args, (ROOT, "fetch", "--no-tags", "--depth=1", "origin", BASE))
        with patch.object(ci_scope, "git", side_effect=(b"", b"proxy-patch/README.md\0")) as command:
            self.assertTrue(ci_scope.wrapper_only(ROOT, "https://github.com", "treyturner/coder", "push", "0" * 40, "refs/heads/proxy-patch/v2.37.4"))
            self.assertEqual(command.call_args_list[0].args, (ROOT, "fetch", "--no-tags", "--depth=1", "origin", "main"))

    def test_invalid_comparisons_fail_the_scope_gate(self):
        for base in ("", "--all", "HEAD", "1" * 39, "1" * 40 + "\n"):
            with self.subTest(base=base), patch.object(ci_scope, "git") as command:
                with self.assertRaises(RuntimeError):
                    ci_scope.wrapper_only(ROOT, "https://github.com", "treyturner/coder", "pull_request", base)
                command.assert_not_called()
        with patch.object(ci_scope, "git", side_effect=RuntimeError("synthetic git failure")):
            with self.assertRaises(RuntimeError):
                ci_scope.wrapper_only(ROOT, "https://github.com", "treyturner/coder", "push", BASE, "refs/heads/main")

    def test_every_skipped_change_starts_patch_verification(self):
        workflow = (ROOT.parent / ".github/workflows/proxy-patch-verify.yml").read_text()
        branches = json.loads(re.search(r"branches: (\[.*\])", workflow).group(1))
        self.assertEqual(branches, list(ci_scope.PUSH_BRANCHES))
        filters = re.findall(r"paths: (\[.*\])", workflow)
        self.assertEqual(len(filters), 2)
        for paths in filters:
            self.assertEqual(set(json.loads(paths)), {"proxy-patch/**", *ci_scope.ROOT_FILES})
        for ref in ("refs/heads/release/2.37", "refs/heads/unrelated", "refs/tags/v2.37.4", ""):
            with self.subTest(ref=ref), patch.object(ci_scope, "git") as command:
                self.assertFalse(ci_scope.wrapper_only(ROOT, "https://github.com", "treyturner/coder", "push", BASE, ref))
                command.assert_not_called()
