"""Offline checks for publication approval and destination binding."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("publish", ROOT / "scripts/publish.py")
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)


class PublicationTests(unittest.TestCase):
    def test_approval_and_destination_binding(self):
        qualified = publish.PUBLICATION
        index = qualified["registry_index_digest"]
        manifest = qualified["platform_manifest_digest"]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            actual = {key: qualified[key] for key in ("registry_index_digest", "platform_manifest_digest", "binary_sha256")}
            (output / "image-digests.json").write_text(json.dumps(actual))
            # Artifact graph validation has its own real OCI regression suite.
            with patch.object(publish.pipeline, "verify_image") as verify:
                self.assertEqual(publish.approved(output, index, manifest), actual)
                verify.assert_called_once_with(output)
                for approved_index, approved_manifest in ((None, None), ("sha256:" + "0" * 64, manifest), (index, "sha256:" + "0" * 64)):
                    with self.subTest(index=approved_index, manifest=approved_manifest):
                        with self.assertRaises(RuntimeError):
                            publish.approved(output, approved_index, approved_manifest)
                for key, value in (("tag", "2.37.4-proxyfix-2"), ("repositories", ["ghcr.io/another-owner/coder"]), ("binary_sha256", "0" * 64)):
                    changed = copy.deepcopy(qualified)
                    changed[key] = value
                    with self.subTest(tampered_field=key), patch.object(publish, "PUBLICATION", changed):
                        with self.assertRaises(RuntimeError):
                            publish.approved(output, index, manifest)

    def test_no_publish_subprocess_without_approval(self):
        with patch("sys.argv", ["publish.py", "publish"]), patch.object(publish.pipeline, "verify_image"), patch.object(publish, "approved", side_effect=RuntimeError("approval missing")), patch.object(publish.subprocess, "run") as run:
            with self.assertRaises(RuntimeError):
                publish.main()
            run.assert_not_called()
