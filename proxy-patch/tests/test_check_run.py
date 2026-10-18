"""Hermetic checks for accepted source workflow runs."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_run", ROOT / "scripts/check_run.py")
check_run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_run)


class SourceRunTests(unittest.TestCase):
    def test_trusted_successful_run_required(self):
        valid = {
            "status": "completed", "conclusion": "success", "event": "push",
            "path": ".github/workflows/proxy-patch-verify.yml",
            "repository": {"full_name": "treyturner/coder"},
            "head_repository": {"full_name": "treyturner/coder"},
            "head_branch": "proxy-patch/v2.37.4",
        }
        invalid = []
        for key, value in (("status", "in_progress"), ("conclusion", "failure"), ("event", "pull_request"), ("path", ".github/workflows/ci.yaml"), ("head_repository", {"full_name": "another-owner/coder"}), ("repository", {"full_name": "another-owner/coder"}), ("head_branch", "unreviewed")):
            changed = copy.deepcopy(valid)
            changed[key] = value
            invalid.append(changed)
        suffixed = {**valid, "path": valid["path"] + "@proxy-patch/v2.37.4"}
        for run in [valid, suffixed, *invalid]:
            with self.subTest(run=run), patch("sys.argv", ["check_run.py", "123"]), patch.dict("os.environ", {"GH_TOKEN": "synthetic-actions-read-token", "GITHUB_SERVER_URL": "https://github.com"}, clear=True), patch.object(check_run, "open_api", return_value=io.BytesIO(json.dumps(run).encode())), contextlib.redirect_stdout(io.StringIO()):
                if run in (valid, suffixed):
                    check_run.main()
                else:
                    with self.assertRaises(RuntimeError):
                        check_run.main()

    def test_invalid_run_id_never_contacts_github(self):
        for value in ("0", "-1", "../123", "123?redirect=elsewhere", "", "123\n456"):
            with self.subTest(value=value), patch("sys.argv", ["check_run.py", value]), patch.object(check_run, "open_api") as request:
                with self.assertRaises(RuntimeError):
                    check_run.main()
                request.assert_not_called()

    def test_mirror_token_never_sent_to_github(self):
        for origin in ("https://forgejo.treyturner.info", "", "https://github.example.invalid"):
            with self.subTest(origin=origin), patch("sys.argv", ["check_run.py", "123"]), patch.dict("os.environ", {"GITHUB_SERVER_URL": origin, "GH_TOKEN": "synthetic-mirror-token"}, clear=True), patch.object(check_run, "open_api") as request:
                with self.assertRaises(RuntimeError):
                    check_run.main()
                request.assert_not_called()
