"""Command failures expose useful categories without copying untrusted output."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pipeline", ROOT / "scripts/pipeline.py")
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


class DiagnosticTests(unittest.TestCase):
    def test_compile_failure_retains_category_and_local_source_location(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "main.go").write_text("package main\n")
            output = b"main.go:17:9: undefined: fixture-secret\nhttps://fixture-secret@proxy.invalid?fixture-secret\n"
            result = subprocess.CompletedProcess(["go", "build"], 1, b"", output)
            captured = io.StringIO()
            with patch.object(pipeline, "EVIDENCE_DIRECTORY", root / "evidence"), patch.object(pipeline.subprocess, "run", return_value=result), contextlib.redirect_stderr(captured):
                with self.assertRaisesRegex(pipeline.PipelineError, "undefined identifier"):
                    pipeline.run(["go", "build"], cwd=root)
            text = (root / "evidence/command-failure.json").read_text()
            self.assertNotIn("fixture-secret", text + captured.getvalue())
            self.assertEqual(json.loads(text)["locations"], [{"file": "main.go", "line": 17}])

    def test_network_error_text_is_reduced_to_fixed_categories(self):
        result = pipeline.safe_diagnostics(b"x509: fixture-secret certificate verification failed\nno such host: fixture-secret")
        self.assertEqual(result["categories"], ["DNS", "TLS verification"])
        self.assertNotIn("fixture-secret", json.dumps(result))

    def test_external_paths_and_unknown_output_are_not_echoed(self):
        with tempfile.TemporaryDirectory() as scratch:
            result = pipeline.safe_diagnostics(b"/outside/fixture-secret.go:5: undefined: fixture-secret\nfixture-secret", Path(scratch))
        self.assertEqual(result["locations"], [])
        self.assertNotIn("fixture-secret", json.dumps(result))
