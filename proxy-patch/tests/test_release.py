"""Hermetic build-record retention and protected GitHub transport checks."""
import copy
import importlib.util
import io
from pathlib import Path
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release", ROOT / "scripts/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
COMMIT = "1" * 40
TAG = "proxy-patch-2.37.4-proxyfix-1"
FILES = {"sources.tar.gz": b"synthetic source and notices", "SHA256SUMS": b"synthetic checksums"}


class Fixture:
    def __init__(self):
        self.reference = self.release = None
        self.assets = {}
        self.writes = []
        self.fail_after = None
        self.corrupt = False
        self.release_pages = None

    def api(self, path, method="GET", data=None, absent=False):
        if method != "GET":
            self.writes.append((method, path))
        if path.startswith("/git/ref/tags/"):
            result = self.reference
        elif path == "/git/refs":
            self.reference = {"object": {"type": "commit", "sha": data["sha"]}}
            result = self.reference
        elif path.startswith("/releases/tags/"):
            result = self.release if self.release and not self.release["draft"] else None
        elif path.startswith("/releases?per_page=100&page="):
            page = int(path.split("=")[-1])
            result = self.release_pages[page - 1] if self.release_pages else ([self.release] if self.release else [])
        elif path == "/releases":
            self.release = {**data, "id": 10}
            result = self.release
        elif path == "/releases/10/assets?per_page=100":
            result = [{"id": index, "name": name, "size": len(value), "state": "uploaded"} for index, (name, value) in enumerate(self.assets.items(), 1)]
        elif path == "/releases/10" and method == "PATCH":
            self.release.update(data)
            result = self.release
        elif path == "/releases/10" and method == "GET":
            result = self.release
        else:
            raise AssertionError("unexpected synthetic API operation")
        return copy.deepcopy(result)

    def upload(self, release_id, name, data):
        if self.fail_after is not None and len(self.assets) >= self.fail_after:
            raise RuntimeError("synthetic interrupted upload")
        self.writes.append(("upload", name))
        self.assets[name] = data

    def download(self, asset_id, size, authenticated=True, tag=None, name=None):
        if not authenticated:
            assert self.release["draft"] is False
        value = list(self.assets.values())[asset_id - 1]
        return b"corrupt" if self.corrupt else value


class ReleaseTests(unittest.TestCase):
    def test_complete_public_delivery_and_idempotent_retry(self):
        client = Fixture()
        release.retain(client, FILES, TAG, COMMIT, "synthetic binding")
        self.assertFalse(client.release["draft"])
        self.assertEqual(client.release["make_latest"], "false")
        self.assertEqual(client.assets, FILES)
        writes = list(client.writes)
        release.retain(client, FILES, TAG, COMMIT, "synthetic binding")
        self.assertEqual(client.writes, writes)

    def test_identical_payload_retries_preserve_original_tag_across_commits(self):
        client = Fixture()
        original = release.retain(client, FILES, TAG, COMMIT, "stable payload binding")
        writes = list(client.writes)
        retry = release.retain(client, FILES, TAG, "2" * 40, "stable payload binding")
        self.assertEqual(original, COMMIT)
        self.assertEqual(retry, COMMIT)
        self.assertEqual(client.writes, writes)
        self.assertEqual(client.reference["object"]["sha"], COMMIT)

    def test_bare_tag_at_another_commit_has_no_payload_binding(self):
        client = Fixture()
        client.reference = {"object": {"type": "commit", "sha": "2" * 40}}
        with self.assertRaisesRegex(RuntimeError, "bare release tag"):
            release.retain(client, FILES, TAG, COMMIT, "stable payload binding")
        self.assertEqual(client.writes, [])

    def test_partial_draft_retries_across_commits_without_replacing_assets(self):
        client = Fixture()
        client.fail_after = 1
        with self.assertRaises(RuntimeError):
            release.retain(client, FILES, TAG, COMMIT, "stable payload binding")
        first = list(client.assets)[0]
        client.fail_after = None
        self.assertEqual(release.retain(client, FILES, TAG, "2" * 40, "stable payload binding"), COMMIT)
        self.assertEqual(client.writes.count(("upload", first)), 1)

    def test_release_identity_is_payload_based(self):
        actual = {"registry_index_digest": "sha256:" + "1" * 64, "platform_manifest_digest": "sha256:" + "2" * 64}
        first = release.release_body(FILES, actual)
        self.assertNotIn("Verification run:", first)
        self.assertNotIn("Source integration commit:", first)
        changed = {**FILES, "sources.tar.gz": b"different source"}
        self.assertNotEqual(first, release.release_body(changed, actual))

    def test_tag_movement_during_readback_is_rejected(self):
        client = Fixture()
        release.retain(client, FILES, TAG, COMMIT, "stable payload binding")
        download = client.download
        def move_tag(*args, **kwargs):
            client.reference["object"]["sha"] = "3" * 40
            return download(*args, **kwargs)
        with patch.object(client, "download", side_effect=move_tag):
            with self.assertRaisesRegex(RuntimeError, "tag differs"):
                release.retain(client, FILES, TAG, "2" * 40, "stable payload binding")

    def test_interrupted_draft_resumes_without_replacing_bytes(self):
        client = Fixture()
        client.fail_after = 1
        with self.assertRaises(RuntimeError):
            release.retain(client, FILES, TAG, COMMIT, "synthetic binding")
        self.assertTrue(client.release["draft"])
        self.assertIsNone(client.api("/releases/tags/" + TAG))
        retained = list(client.assets)
        client.fail_after = None
        release.retain(client, FILES, TAG, COMMIT, "synthetic binding")
        for name in retained:
            self.assertEqual(client.writes.count(("upload", name)), 1)

    def test_draft_lookup_is_paginated_and_rejects_duplicate_tags(self):
        client = Fixture()
        client.fail_after = 1
        with self.assertRaises(RuntimeError):
            release.retain(client, FILES, TAG, COMMIT, "synthetic binding")
        client.release_pages = [[{"tag_name": "unrelated"}] * 100, [copy.deepcopy(client.release)]]
        client.fail_after = None
        release.retain(client, FILES, TAG, COMMIT, "synthetic binding")
        self.assertFalse(client.release["draft"])
        client.release_pages = [[copy.deepcopy(client.release), copy.deepcopy(client.release)]]
        writes = list(client.writes)
        with self.assertRaises(RuntimeError):
            release.retain(client, FILES, TAG, COMMIT, "synthetic binding")
        self.assertEqual(client.writes, writes)

    def test_conflicting_tag_metadata_names_or_bytes_stop_before_writes(self):
        for conflict in ("tag", "body", "name", "bytes", "missing-public"):
            with self.subTest(conflict=conflict):
                client = Fixture()
                release.retain(client, FILES, TAG, COMMIT, "synthetic binding")
                if conflict == "tag":
                    client.reference["object"]["type"] = "tag"
                elif conflict == "body":
                    client.release["body"] = "different binding"
                elif conflict == "name":
                    client.assets["unexpected-file"] = b"unapproved"
                elif conflict == "bytes":
                    name = next(iter(client.assets))
                    client.assets[name] = b"X" * len(client.assets[name])
                else:
                    client.assets.pop(next(iter(client.assets)))
                writes = list(client.writes)
                with self.assertRaises(RuntimeError):
                    release.retain(client, FILES, TAG, COMMIT, "synthetic binding")
                self.assertEqual(client.writes, writes)

    def test_failed_readback_keeps_release_draft(self):
        client = Fixture()
        client.corrupt = True
        with self.assertRaises(RuntimeError):
            release.retain(client, FILES, TAG, COMMIT, "synthetic binding")
        self.assertTrue(client.release["draft"])
        self.assertNotIn(("PATCH", "/releases/10"), client.writes)

    def test_asset_redirect_never_forwards_authentication(self):
        client = release.GitHub("synthetic-github-token")
        calls = []
        def open_request(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 302, "synthetic", {"Location": "https://release-assets.githubusercontent.com/synthetic?signature=synthetic"}, None)
            return io.BytesIO(b"payload")
        with patch.object(client.opener, "open", side_effect=open_request):
            self.assertEqual(client.download(1, 7), b"payload")
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer synthetic-github-token")
        self.assertIsNone(calls[1].get_header("Authorization"))
        for destination in ("http://release-assets.githubusercontent.com/file", "https://untrusted.invalid/file", "https://user:password@release-assets.githubusercontent.com/file"):
            with self.subTest(destination=destination), patch.object(client.opener, "open") as request:
                with self.assertRaises(RuntimeError):
                    client.request(destination, binary=True, authenticated=False)
                request.assert_not_called()
        with self.assertRaises(RuntimeError):
            client.request("https://release-assets.githubusercontent.com/file")

    def test_public_delivery_uses_web_download_without_anonymous_api_calls(self):
        client = release.GitHub("synthetic-github-token")
        calls = []
        def open_request(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 302, "synthetic", {"Location": "https://release-assets.githubusercontent.com/synthetic?signature=synthetic"}, None)
            return io.BytesIO(b"payload")
        with patch.object(client.opener, "open", side_effect=open_request):
            self.assertEqual(client.download(1, 7, authenticated=False, tag=TAG, name="sources.tar.gz"), b"payload")
        self.assertEqual(calls[0].full_url, release.PUBLIC + TAG + "/sources.tar.gz")
        for request in calls:
            self.assertIsNone(request.get_header("Authorization"))
            self.assertFalse(request.full_url.startswith(release.API))
        with self.assertRaisesRegex(RuntimeError, "anonymous"):
            client.request(release.API + "/releases/assets/1", authenticated=False)

    def test_public_redirect_to_another_repository_is_rejected(self):
        client = release.GitHub("synthetic-github-token")
        redirect = urllib.error.HTTPError(release.PUBLIC + TAG + "/sources.tar.gz", 302, "synthetic", {"Location": "https://github.com/another/repo/releases/download/tag/asset"}, None)
        with patch.object(client.opener, "open", side_effect=redirect) as request:
            with self.assertRaisesRegex(RuntimeError, "untrusted"):
                client.download(1, 7, authenticated=False, tag=TAG, name="sources.tar.gz")
        self.assertEqual(request.call_count, 1)

    def test_wrong_origin_or_source_never_starts_delivery(self):
        arguments = ["release.py", "--work", "/unused", "--verification-run-id", "123", "--approved-index", "synthetic", "--approved-manifest", "synthetic"]
        with patch("sys.argv", arguments), patch.dict("os.environ", {}, clear=True), patch.object(release.publisher, "approved") as verify, patch.object(release, "retain") as retain:
            with self.assertRaises(RuntimeError):
                release.main()
            verify.assert_not_called()
            retain.assert_not_called()
        env = {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "treyturner/coder", "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": COMMIT}
        for source in ({"head_sha": "2" * 40, "head_branch": "main"}, {"head_sha": COMMIT, "head_branch": "proxy-patch/v2.37.4"}):
            with self.subTest(source=source), patch("sys.argv", arguments), patch.dict("os.environ", env, clear=True), patch.object(release.publisher, "approved", return_value={}), patch.object(release.source_run, "verified_run", return_value=source), patch.object(release, "retain") as retain:
                with self.assertRaises(RuntimeError):
                    release.main()
                retain.assert_not_called()

    def test_registry_job_requires_companion_success(self):
        workflow = (ROOT.parent / ".github/workflows/proxy-patch-publish.yml").read_text()
        publish = workflow.split("\n  publish:\n", 1)[1]
        self.assertIn("needs: [prepare, companions]", publish)
        self.assertNotIn("if: always()", publish.split("    steps:", 1)[0])
