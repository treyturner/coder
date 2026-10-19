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
                for key, value in (("tag", "2.37.4-proxyfix-2"), ("repositories", ["https://ghcr.io/invalid/coder"]), ("binary_sha256", "0" * 64)):
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

    def test_stale_go_files_never_enter_publisher_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            old = work / "tools/registry-publisher"
            old.mkdir(parents=True)
            (old / "stale.go").write_text("package main\nfunc init() { panic(\"unreviewed\") }\n")
            staged = []

            def run(command, cwd=None, **kwargs):
                if cwd is not None:
                    source = Path(cwd)
                    self.assertNotEqual(source, old)
                    self.assertEqual({path.name for path in source.iterdir()}, {"go.mod", "go.sum", "main.go", "main_internal_test.go"})
                    staged.append(source)
                if command[1] == "list":
                    return publish.PUBLICATION["registry_client_version"].encode()
                if command[1] == "build":
                    Path(command[3]).write_bytes(b"synthetic binary")
                return b""

            with patch.object(publish.pipeline, "bootstrap", return_value=Path("go")), patch.object(publish.pipeline, "build_env", return_value={}), patch.object(publish.pipeline, "run", side_effect=run):
                publish.prepare(work)
            self.assertTrue(staged)
            self.assertTrue(all(not path.exists() for path in staged))
            self.assertEqual((old / "publish").read_bytes(), b"synthetic binary")
