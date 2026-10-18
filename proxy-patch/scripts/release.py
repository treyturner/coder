#!/usr/bin/env python3
"""Retain the exact qualified payload as verified, versioned GitHub release assets."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


publisher = module("publish")
source_run = module("check_run")
API = "https://api.github.com/repos/treyturner/coder"
UPLOAD = "https://uploads.github.com/repos/treyturner/coder"
PUBLIC = "https://github.com/treyturner/coder/releases/download/"
ASSET_HOSTS = frozenset(("release-assets.githubusercontent.com", "objects.githubusercontent.com"))
PAYLOAD = frozenset((
    "SHA256SUMS", "coder", "coder-cli.oci.tar", "image-digests.json", "module-info.txt",
    "provenance.json", "recipe.tar.gz", "runtime-index.json", "runtime-manifest.json",
    "sbom.cdx.json", "sources.lock.json", "sources.tar.gz",
))


class ReleaseError(RuntimeError):
    """A locally generated release failure safe to show without exception causes."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, url, method="GET", data=None, binary=False, authenticated=True):
        parsed = urllib.parse.urlsplit(url)
        trusted_api = url.startswith(API + "/") or url.startswith(UPLOAD + "/")
        trusted_public = url.startswith(PUBLIC) and not parsed.query
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment:
            raise ReleaseError("untrusted release endpoint")
        if authenticated and not trusted_api:
            raise ReleaseError("refusing authentication outside the fixed release API")
        if not authenticated and trusted_api:
            raise ReleaseError("refusing anonymous release API download")
        if not trusted_api and not trusted_public and parsed.hostname not in ASSET_HOSTS:
            raise ReleaseError("untrusted asset storage endpoint")
        headers = {"Accept": "application/octet-stream" if binary else "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        if data is not None:
            headers["Content-Type"] = "application/octet-stream" if binary else "application/json"
            if not binary:
                data = json.dumps(data).encode()
        return self.opener.open(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=120)

    def api(self, path, method="GET", data=None, absent=False):
        try:
            with self.request(API + path, method, data) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            missing = absent and method == "GET" and error.code == 404
            error.close()
            if missing:
                return None
            raise ReleaseError("release API request failed") from None

    def upload(self, release_id, name, data):
        url = UPLOAD + "/releases/" + identifier(release_id) + "/assets?name=" + urllib.parse.quote(name, safe="")
        with self.request(url, "POST", data, binary=True) as response:
            return json.load(response)

    def download(self, asset_id, size, authenticated=True, tag=None, name=None):
        if authenticated:
            url = API + "/releases/assets/" + identifier(asset_id)
        else:
            if not tag or not name:
                raise ReleaseError("public asset identity is unavailable")
            url = PUBLIC + urllib.parse.quote(tag, safe="") + "/" + urllib.parse.quote(name, safe="")
        for _ in range(4):
            try:
                response = self.request(url, binary=True, authenticated=authenticated)
            except urllib.error.HTTPError as error:
                redirected = error.code in (301, 302, 303, 307, 308)
                location = error.headers.get("Location", "")
                status = error.code
                error.close()
                if not redirected or not location:
                    raise ReleaseError("release asset download failed (HTTP " + str(status) + ")") from None
                # Every redirected request is revalidated and carries no token.
                url = urllib.parse.urljoin(url, location)
                authenticated = False
                continue
            with response:
                value = response.read(size + 1)
            if len(value) != size:
                raise ReleaseError("release asset size differs")
            return value
        raise ReleaseError("release asset redirect limit exceeded")


def identifier(value):
    if type(value) is not int or value < 1:
        raise ReleaseError("invalid release resource identifier")
    return str(value)


def find_release(client, tag):
    matches = []
    for page in range(1, 101):
        releases = client.api("/releases?per_page=100&page=" + str(page))
        if not isinstance(releases, list):
            raise ReleaseError("invalid release listing")
        matches.extend(item for item in releases if item.get("tag_name") == tag)
        if len(matches) > 1:
            raise ReleaseError("duplicate release tags conflict")
        if len(releases) < 100:
            return matches[0] if matches else None
    raise ReleaseError("release listing exceeded the verified limit")


def retain(client, files, tag, commit, body):
    reference = client.api("/git/ref/tags/" + tag, absent=True)
    # Listing with push access includes drafts; the tag endpoint is for
    # published releases and cannot establish whether a draft already exists.
    release = find_release(client, tag)
    if reference is not None:
        obj = reference.get("object", {})
        if obj.get("type") != "commit" or not re.fullmatch(r"[0-9a-f]{40}", obj.get("sha", "")):
            raise ReleaseError("existing release tag conflicts")
        if release is None and obj["sha"] != commit:
            raise ReleaseError("bare release tag is not bound to the current verified commit")
    tag_commit = reference["object"]["sha"] if reference is not None else commit
    expected = {"tag_name": tag, "name": "Coder " + tag + " build records", "body": body, "prerelease": False}
    if release is not None and any(release.get(key) != value for key, value in expected.items()):
        raise ReleaseError("existing release metadata conflicts")
    if release is not None and reference is None:
        raise ReleaseError("existing release tag is unavailable")
    if reference is None:
        client.api("/git/refs", "POST", {"ref": "refs/tags/" + tag, "sha": commit})
    if release is None:
        release = client.api("/releases", "POST", {**expected, "target_commitish": tag_commit, "draft": True, "make_latest": "false"})
    release_id = identifier(release["id"])

    def verify_assets(complete, public=False):
        assets = client.api("/releases/" + release_id + "/assets?per_page=100")
        names = [asset.get("name") for asset in assets]
        if len(names) != len(set(names)) or not set(names).issubset(files) or (complete and set(names) != set(files)):
            raise ReleaseError("release asset names conflict")
        for asset in assets:
            data = files[asset["name"]]
            if asset.get("state") != "uploaded" or asset.get("size") != len(data):
                raise ReleaseError("release asset metadata conflicts")
            if client.download(asset["id"], len(data), authenticated=not public, tag=tag if public else None, name=asset["name"] if public else None) != data:
                raise ReleaseError("release asset checksum differs")
        return set(names)

    present = verify_assets(complete=not release["draft"])
    if release["draft"]:
        for name in sorted(set(files) - present):
            client.upload(release["id"], name, files[name])
        verify_assets(complete=True)
        client.api("/releases/" + release_id, "PATCH", {"draft": False, "make_latest": "false"})
    published = client.api("/releases/" + release_id)
    if published.get("id") != release["id"] or published.get("draft") is not False or any(published.get(key) != value for key, value in expected.items()):
        raise ReleaseError("published release metadata differs")
    final_ref = client.api("/git/ref/tags/" + tag)
    if final_ref.get("object", {}).get("sha") != tag_commit or final_ref.get("object", {}).get("type") != "commit":
        raise ReleaseError("published release tag differs")
    verify_assets(complete=True, public=True)
    return tag_commit


def release_body(files, actual):
    payload = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return (
        "Qualified Linux amd64 Coder CLI build records. Registry publication is confirmed only by digest readback and its separate workflow receipt.\n\n"
        + "Payload SHA256: " + identity + "\n"
        + "SHA256SUMS SHA256: " + hashlib.sha256(files["SHA256SUMS"]).hexdigest() + "\n"
        + "Registry index: " + actual["registry_index_digest"] + "\nPlatform manifest: " + actual["platform_manifest_digest"] + "\n"
        + "\n".join(repository + ":" + publisher.PUBLICATION["tag"] for repository in publisher.PUBLICATION["repositories"])
        + "\n\nUse sources.tar.gz for the exact patched source and license notices, and recipe.tar.gz for the build recipe. The generated GitHub source archive contains the original integration wrapper. Retry run and commit provenance is retained in workflow receipts; an existing release tag is never moved.\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--verification-run-id", required=True)
    parser.add_argument("--approved-index", required=True)
    parser.add_argument("--approved-manifest", required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_SERVER_URL") != "https://github.com" or os.environ.get("GITHUB_REPOSITORY") != "treyturner/coder" or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ReleaseError("release delivery requires the intended GitHub main branch")
    work = args.work.resolve()
    output = work / "dist"
    actual = publisher.approved(output, args.approved_index, args.approved_manifest)
    run = source_run.verified_run(args.verification_run_id)
    commit = run.get("head_sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or run.get("head_branch") != "main" or commit != os.environ.get("GITHUB_SHA"):
        raise ReleaseError("release delivery requires verification of the current main commit")
    files = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
    if set(files) != PAYLOAD:
        raise ReleaseError("qualified release payload names differ")
    tag = "proxy-patch-" + publisher.PUBLICATION["tag"]
    body = release_body(files, actual)
    tag_commit = retain(GitHub(os.environ["GH_TOKEN"]), files, tag, commit, body)
    evidence = work / "evidence/companions.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps({"status": "pass", "release_url": "https://github.com/treyturner/coder/releases/tag/" + tag, "verification_run_id": args.verification_run_id, "source_commit": commit, "release_tag_commit": tag_commit, "payload_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}, "registry_index_digest": actual["registry_index_digest"]}, indent=2) + "\n")
    print("Verified durable build records:", tag)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Signed download URLs, response bodies and authentication never enter logs.
        print("release delivery stopped:", str(error) if isinstance(error, ReleaseError) else type(error).__name__, file=sys.stderr)
        sys.exit(1)
