"""Verify fork workflow selection without using real Actions administration."""
import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("configure", ROOT / "scripts/configure_workflows.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


class WorkflowTests(unittest.TestCase):
    def test_check_reports_inherited_workflows_without_writes(self):
        values = [{"path": ".github/workflows/ci.yaml", "id": 1, "state": "active"}]
        with patch.object(configure, "workflows", return_value=values), patch.object(configure, "request") as request, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "--apply"):
                configure.configure()
            request.assert_not_called()

    def test_apply_disables_inherited_workflows_and_enables_patch_workflows(self):
        before = [
            {"path": ".github/workflows/ci.yaml", "id": 1, "state": "active"},
            {"path": ".github/workflows/new-upstream.yml", "id": 2, "state": "active"},
            {"path": ".github/workflows/proxy-patch-verify.yml", "id": 3, "state": "disabled_manually"},
        ]
        after = [{**item, "state": "active" if item["path"] in configure.ENABLED else "disabled_manually"} for item in before]
        with patch.object(configure, "workflows", side_effect=[before, after]), patch.object(configure, "request") as request, contextlib.redirect_stdout(io.StringIO()):
            configure.configure(True)
        self.assertEqual([call.args for call in request.call_args_list], [
            (configure.PREFIX + "/1/disable", "PUT"),
            (configure.PREFIX + "/2/disable", "PUT"),
            (configure.PREFIX + "/3/enable", "PUT"),
        ])

    def test_invalid_workflow_identity_never_becomes_an_api_path(self):
        for identifier, path in (("1/enable", ".github/workflows/ci.yaml"), (1, "../outside.yml")):
            with self.subTest(identifier=identifier, path=path), patch.object(configure, "request", return_value={"workflows": [{"id": identifier, "path": path, "state": "active"}]}):
                with self.assertRaisesRegex(RuntimeError, "identity"):
                    configure.workflows()

    def test_workflow_pages_are_requested_without_new_cli_pagination_flags(self):
        first = [{"id": number + 1, "path": ".github/workflows/upstream-" + str(number) + ".yml", "state": "disabled_manually"} for number in range(100)]
        last = [{"id": 101, "path": ".github/workflows/proxy-patch-verify.yml", "state": "active"}]
        with patch.object(configure, "request", side_effect=[{"workflows": first}, {"workflows": last}]) as request:
            self.assertEqual(len(configure.workflows()), 101)
        self.assertEqual([call.args[0] for call in request.call_args_list], [configure.PREFIX + "?per_page=100&page=1", configure.PREFIX + "?per_page=100&page=2"])
