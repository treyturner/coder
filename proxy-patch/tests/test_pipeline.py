"""Offline regression checks for artifact binding and deterministic packaging."""

import copy
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

CONTROL = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pipeline", CONTROL / "scripts/pipeline.py")
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


class PackagingTests(unittest.TestCase):
    def setUp(self):
        scratch = CONTROL.parent / ".build/pipeline-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.control = self.work / "control"
        self.control.mkdir()
        for name in pipeline.RECIPE_FILES:
            if name != "sources.lock.json":
                destination = self.control / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(CONTROL / name, destination)
        self.parent = self.work / "assembly"
        self.lock = copy.deepcopy(pipeline.LOCK)
        for name, source in self.lock["sources"].items():
            repo = self.parent / name
            repo.mkdir(parents=True)
            (repo / "LICENSE").write_text("Public synthetic license fixture.\n")
            if name == "coder":
                (repo / "LICENSE.enterprise").write_text("Synthetic enterprise notice.\n")
            subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "add", "--all"], cwd=repo, check=True, capture_output=True)
            source["patched_tree"] = subprocess.check_output(["git", "write-tree"], cwd=repo).decode().strip()

        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            pipeline.add_tar(tar, "etc/ssl/certs/ca-certificates.crt", b"synthetic public CA fixture")
        compressed = gzip.compress(raw.getvalue(), mtime=0)
        config = pipeline.canonical({
            "os": "linux", "architecture": "amd64", "config": {},
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + pipeline.digest(raw.getvalue())]},
        })

        def descriptor(data, media_type):
            return {"digest": "sha256:" + pipeline.digest(data), "size": len(data), "mediaType": media_type}

        manifest = pipeline.canonical({
            "schemaVersion": 2,
            "config": descriptor(config, pipeline.OCI + "config.v1+json"),
            "layers": [descriptor(compressed, pipeline.OCI + "layer.v1.tar+gzip")],
        })
        index = pipeline.canonical({"schemaVersion": 2, "manifests": [{
            **descriptor(manifest, pipeline.OCI + "manifest.v1+json"),
            "platform": self.lock["platform"],
        }]})
        self.lock["runtime"]["manifest_digest"] = "sha256:" + pipeline.digest(manifest)
        self.lock["runtime"]["index_digest"] = "sha256:" + pipeline.digest(index)
        self.control.joinpath("sources.lock.json").write_text(json.dumps(self.lock, indent=2) + "\n")
        artifacts = {"sha256:" + pipeline.digest(data): data for data in (compressed, config, manifest, index)}

        def download(url, expected=None):
            data = artifacts[url.rsplit("/", 1)[1]]
            pipeline.check("sha256:" + pipeline.digest(data) == expected, "fixture checksum mismatch")
            return data

        self.info = (
            "coder: go1.26.5\n"
            "\tdep\ttailscale.com\tv1.1.1\n"
            "\t=>\t../tailscale\t(devel)\n"
            "\tdep\tnhooyr.io/websocket\tv1.8.11\n"
            "\t=>\tgithub.com/coder/websocket\tv1.8.14\th1:synthetic\n"
            "\tdep\tgolang.org/x/net\tv0.58.0\th1:synthetic\n"
        )
        for target, value in (("ROOT", self.control), ("LOCK", self.lock), ("LOCK_BYTES", (self.control / "sources.lock.json").read_bytes()), ("download", download)):
            guard = patch.object(pipeline, target, value)
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch.object(pipeline, "binary_info", return_value=self.info)
        guard.start()
        self.addCleanup(guard.stop)

    def payload(self, name="dist"):
        output = self.work / name
        output.mkdir()
        (output / "coder").write_bytes(b"synthetic executable fixture")
        (output / "module-info.txt").write_text(self.info)
        shutil.copyfile(self.control / "sources.lock.json", output / "sources.lock.json")
        for name in self.lock["sources"]:
            directory = output / "licenses" / name
            directory.mkdir(parents=True)
            for path in (self.parent / name).glob("LICENSE*"):
                shutil.copyfile(path, directory / path.name)
        (output / "sbom.cdx.json").write_bytes(pipeline.canonical({"components": pipeline.effective_components(self.info)}))
        (output / "recipe.tar.gz").write_bytes(pipeline.recipe_bytes())
        (output / "provenance.json").write_bytes(pipeline.canonical(pipeline.expected_provenance(output)))
        return output

    def image(self, name="dist"):
        output = self.payload(name)
        pipeline.package_image(self.work, self.parent, output)
        return output

    def test_reproducible_image_and_sources(self):
        first, second = self.image("first"), self.image("second")
        for name in ("coder-cli.oci.tar", "sources.tar.gz", "image-digests.json", "SHA256SUMS"):
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())

    def test_verification_does_not_recompress_recipe(self):
        output = self.image()
        with patch.object(pipeline, "recipe_bytes", side_effect=AssertionError("verification must not recompress")):
            pipeline.verify_image(output)

    def test_editorial_changes_do_not_invalidate_build_recipe(self):
        output = self.image()
        (self.control / "README.md").write_text("An editorial correction.\n")
        pipeline.verify_image(output)

    def test_recipe_content_is_checked_even_with_updated_recorded_checksum(self):
        output = self.image()
        archive = output / "recipe.tar.gz"
        archive.write_bytes(gzip.compress(gzip.decompress(archive.read_bytes()) + b"unexpected content", mtime=0))
        (output / "provenance.json").write_bytes(pipeline.canonical(pipeline.expected_provenance(output)))
        pipeline.checksums(output)
        with self.assertRaisesRegex(RuntimeError, "build recipe changed"):
            pipeline.verify_image(output)

    def test_stale_binary_rejected_before_packaging(self):
        output = self.payload()
        (output / "coder").write_bytes(b"different binary")
        with self.assertRaisesRegex(RuntimeError, "not bound"):
            pipeline.package_image(self.work, self.parent, output)
        self.assertFalse((output / "oci").exists())

    def test_new_lock_requires_rebuild(self):
        output = self.payload()
        self.lock["version"] = "2.37.4+proxyfix.2"
        (self.control / "sources.lock.json").write_text(json.dumps(self.lock))
        with self.assertRaisesRegex(RuntimeError, "lock changed"):
            pipeline.package_image(self.work, self.parent, output)

    def test_replacement_inventory_uses_effective_sources(self):
        components = pipeline.effective_components(self.info)
        self.assertEqual(components[0]["name"], "github.com/coder/tailscale")
        self.assertEqual(components[0]["version"], self.lock["sources"]["tailscale"]["commit"] + "+proxyfix.1")
        self.assertEqual(components[1]["name"], "github.com/coder/websocket")
        self.assertEqual(components[1]["version"], "v1.8.14")
        self.assertTrue(any(p["name"] == "info.proxy-patch.patched_tree" for p in components[0]["properties"]))

    def test_dependency_identity_follows_patch_revision(self):
        self.lock["patch_revision"] = "proxyfix.2"
        self.assertTrue(pipeline.effective_components(self.info)[0]["version"].endswith("+proxyfix.2"))

    def test_blob_tampering_rejected(self):
        output = self.image()
        blob = next((output / "oci/blobs/sha256").iterdir())
        blob.write_bytes(blob.read_bytes() + b"changed")
        with self.assertRaisesRegex(RuntimeError, "blob checksum"):
            pipeline.verify_image(output)

    def test_archive_tampering_rejected_even_with_new_checksums(self):
        output = self.image()
        path = output / "coder-cli.oci.tar"
        path.write_bytes(path.read_bytes() + b"changed")
        pipeline.checksums(output)
        with self.assertRaisesRegex(RuntimeError, "digest summary"):
            pipeline.verify_image(output)

    def test_checksums_are_checked(self):
        output = self.image()
        (output / "SHA256SUMS").write_text("changed\n")
        with self.assertRaisesRegex(RuntimeError, "artifact checksums"):
            pipeline.verify_image(output)

    def test_descriptor_sizes_are_checked(self):
        output = self.image()
        index = json.loads((output / "oci/index.json").read_bytes())
        descriptor = index["manifests"][0]
        descriptor["size"] += 1
        with self.assertRaisesRegex(RuntimeError, "descriptor size"):
            pipeline.read_descriptor(output / "oci", descriptor)

    def test_catalog_names_a_distinct_registry_index(self):
        output = self.image()
        summary = json.loads((output / "image-digests.json").read_bytes())
        catalog = json.loads((output / "oci/index.json").read_bytes())
        reference = catalog["manifests"][0]
        self.assertEqual(reference["mediaType"], pipeline.OCI + "index.v1+json")
        self.assertEqual(reference["digest"], summary["registry_index_digest"])
        self.assertNotEqual(reference["digest"], "sha256:" + summary["layout_catalog_sha256"])
        index = json.loads(pipeline.read_descriptor(output / "oci", reference))
        self.assertEqual(index["manifests"][0]["digest"], summary["platform_manifest_digest"])
        self.assertEqual(index["manifests"][0]["platform"], self.lock["platform"])

    def test_catalog_tag_tampering_rejected_with_new_checksums(self):
        output = self.image()
        path = output / "oci/index.json"
        catalog = json.loads(path.read_bytes())
        catalog["manifests"][0]["annotations"]["org.opencontainers.image.ref.name"] = "wrong-tag"
        path.write_bytes(pipeline.canonical(catalog))
        pipeline.checksums(output)
        with self.assertRaisesRegex(RuntimeError, "catalog reference tag"):
            pipeline.verify_image(output)

    def test_catalog_cannot_substitute_platform_manifest_for_index(self):
        output = self.image()
        path = output / "oci/index.json"
        catalog = json.loads(path.read_bytes())
        index = json.loads(pipeline.read_descriptor(output / "oci", catalog["manifests"][0]))
        platform = index["manifests"][0]
        catalog["manifests"][0].update({"digest": platform["digest"], "size": platform["size"]})
        path.write_bytes(pipeline.canonical(catalog))
        pipeline.checksums(output)
        with self.assertRaisesRegex(RuntimeError, "registry image index"):
            pipeline.verify_image(output)

    def test_registry_index_platform_tampering_rejected(self):
        output = self.image()
        path = output / "oci/index.json"
        catalog = json.loads(path.read_bytes())
        index = json.loads(pipeline.read_descriptor(output / "oci", catalog["manifests"][0]))
        index["manifests"][0]["platform"]["architecture"] = "arm64"
        replacement = pipeline.store_blob(output / "oci", pipeline.canonical(index))
        catalog["manifests"][0].update(replacement)
        path.write_bytes(pipeline.canonical(catalog))
        pipeline.checksums(output)
        with self.assertRaisesRegex(RuntimeError, "manifest platform"):
            pipeline.verify_image(output)

    def test_test_environment_excludes_runtime_secrets_and_debug(self):
        with patch.dict(os.environ, {"SSLKEYLOGFILE": "/tmp/unused", "TS_DEBUG_TLS_DIAL": "1", "TS_DEBUG_FAKE_PROXY_AUTH": "synthetic", "CODER_SESSION_TOKEN": "synthetic", "HTTP_PROXY": "http://fixture.invalid", "SSL_CERT_FILE": "/tmp/public-ca"}):
            env = pipeline.build_env(self.work)
        for name in ("SSLKEYLOGFILE", "TS_DEBUG_TLS_DIAL", "TS_DEBUG_FAKE_PROXY_AUTH", "CODER_SESSION_TOKEN"):
            self.assertNotIn(name, env)
        self.assertEqual(env["HTTP_PROXY"], "http://fixture.invalid")
        self.assertEqual(env["SSL_CERT_FILE"], "/tmp/public-ca")


if __name__ == "__main__":
    unittest.main()
