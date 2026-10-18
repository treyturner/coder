#!/usr/bin/env python3
"""Assemble pinned source, test, build and package the Coder proxy CLI locally."""

import argparse
import datetime
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zlib

ROOT = Path(__file__).resolve().parents[1]
PIPELINE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
LOCK_BYTES = (ROOT / "sources.lock.json").read_bytes()
LOCK = json.loads(LOCK_BYTES)
OCI = "application/vnd.oci.image."
BOOTSTRAPPED = set()
EVIDENCE_DIRECTORY = None
RECIPE_FILES = ("README.md", "LICENSE", "Dockerfile", "sources.lock.json", "scripts/pipeline.py",
                "tests/test_pipeline.py", "tests/baseline_test.go.txt", "tests/oci_validate.go.txt", ".github/workflows/verify.yml",
                "patches/coder.patch", "patches/tailscale.patch", "docs/PLAN.md")


class PipelineError(RuntimeError):
    """A locally generated failure message that excludes remote diagnostics."""


def safe_diagnostics(data, cwd=None):
    text = data.decode(errors="replace")
    categories = set()
    for label, pattern in (
        ("syntax error", r"syntax error|expected .*found"),
        ("undefined identifier", r"undefined:|undefined reference"),
        ("type mismatch", r"cannot use |incompatible type|does not implement"),
        ("patch conflict", r"patch does not apply|patch failed:"),
        ("TLS verification", r"x509:|certificate verify failed|certificate verification"),
        ("DNS", r"no such host|name resolution|dial tcp: lookup"),
        ("proxy access", r"proxyconnect|proxy authentication|HTTP (403|407)"),
        ("timeout", r"timed out|deadline exceeded|timeout"),
        ("formatting", r"gofumpt|gci|not gofmt"),
    ):
        if re.search(pattern, text, re.IGNORECASE):
            categories.add(label)
    locations = set()
    root = Path(cwd).resolve() if cwd else None
    if root:
        for name, line in re.findall(r"(?m)^([^\n:]+\.go):([1-9][0-9]*)(?::[0-9]+)?:", text):
            path = (root / name).resolve()
            if path.is_relative_to(root) and path.is_file() and not path.is_symlink():
                relative = path.relative_to(root).as_posix()
                if re.fullmatch(r"[A-Za-z0-9_./-]+\.go", relative):
                    locations.add((relative, int(line)))
    return {"categories": sorted(categories) or ["command failure"], "locations": [{"file": name, "line": line} for name, line in sorted(locations)[:50]]}


def command_failure(args, result, cwd=None):
    executable = Path(args[0]).name
    if any(Path(value).name == "build_go.sh" for value in args[1:]):
        operation = "CLI build"
    elif executable in ("go", "git") and len(args) > 1 and args[1] in ("build", "test", "vet", "mod", "list", "apply", "clone", "fetch", "diff", "tool"):
        operation = executable + " " + args[1]
    else:
        operation = executable if executable in ("gofmt", "golangci-lint", "make", "configure") else "build tool"
    record = {"status": "fail", "operation": operation, "exit_code": result.returncode, **safe_diagnostics(result.stdout + result.stderr, cwd)}
    if EVIDENCE_DIRECTORY is not None:
        EVIDENCE_DIRECTORY.mkdir(parents=True, exist_ok=True)
        (EVIDENCE_DIRECTORY / "command-failure.json").write_bytes(canonical(record))
    print("Verification failure:", json.dumps(record, sort_keys=True), file=sys.stderr)
    raise PipelineError("verification command failed: " + operation + " (" + ", ".join(record["categories"]) + ")")


def check(condition, message):
    if not condition:
        raise PipelineError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def locked_bytes():
    check((ROOT / "sources.lock.json").read_bytes() == LOCK_BYTES, "source lock changed during execution; rerun with frozen inputs")
    expected_version = LOCK["base_version"] + "+" + LOCK["patch_revision"] + "." + LOCK["sources"]["coder"]["commit"][:7]
    check(LOCK["version"] == expected_version and re.fullmatch(r"proxyfix\.[1-9][0-9]*", LOCK["patch_revision"]), "patch revision and CLI version differ")
    return LOCK_BYTES


def run(args, cwd=None, env=None, capture=False, expected=0):
    # Tool failures may include authenticated URLs or remote response text.
    # Never stream them or persist raw diagnostic output in build artifacts.
    result = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, check=False)
    if result.returncode != expected:
        command_failure(args, result, cwd)
    return result.stdout if capture else b""


def download(url, expected=None):
    try:
        req = urllib.request.Request(url, headers={"Accept": ", ".join([
            OCI + "manifest.v1+json", OCI + "index.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ])})
        with urllib.request.urlopen(req, timeout=60) as response:
            data = response.read()
    except (urllib.error.URLError, OSError):
        # URLs can contain registry-issued signed query parameters.
        raise RuntimeError("public artifact download failed") from None
    if expected:
        check(digest(data) == expected.removeprefix("sha256:"), "download checksum mismatch")
    return data


def bootstrap(work):
    check(platform.system() == "Linux" and platform.machine() in ("x86_64", "amd64"), "locked builder requires Linux amd64")
    tool = LOCK["go"]
    directory = work / "toolchains"
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / tool["filename"]
    if not archive.exists():
        archive.write_bytes(download(tool["url"], tool["sha256"]))
    check(digest(archive.read_bytes()) == tool["sha256"], "cached toolchain checksum mismatch")
    binary = directory / "go/bin/go"
    if directory in BOOTSTRAPPED:
        return binary
    if not binary.exists():
        with tarfile.open(archive) as tar:
            tar.extractall(directory, filter="data")
    # A checked archive alone does not attest a previously extracted toolchain.
    with tarfile.open(archive) as tar:
        expected_files = set()
        for member in tar:
            if member.isfile():
                path = directory / member.name
                check(path.is_file() and not path.is_symlink(), "cached toolchain file missing")
                check(digest(path.read_bytes()) == digest(tar.extractfile(member).read()), "cached toolchain file differs from archive")
                expected_files.add(path)
            else:
                check(member.isdir(), "unexpected toolchain archive member")
        actual_files = {p for p in (directory / "go").rglob("*") if not p.is_dir()}
        check(actual_files == expected_files, "cached toolchain contains additional files")
    version = run([str(binary), "version"], env=build_env(work), capture=True).decode().split()
    check(version[2] == tool["version"], "unexpected Go toolchain")
    BOOTSTRAPPED.add(directory)
    return binary


def build_env(work):
    # Keep existing proxy/CA policy; exclude runtime credentials and debug/key
    # logging knobs from tests and builds. Do not print inherited values.
    inherited = ("HOME", "TMPDIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "no_proxy", "all_proxy", "SSL_CERT_FILE",
                 "SSL_CERT_DIR", "GIT_SSL_CAINFO", "GIT_SSL_CAPATH", "CURL_CA_BUNDLE",
                 "REQUESTS_CA_BUNDLE", "GOPROXY", "GOSUMDB")
    env = {key: os.environ[key] for key in inherited if key in os.environ}
    env.update({
        "GOWORK": "off", "GOTOOLCHAIN": "local", "GOMAXPROCS": "4",
        "GOENV": "off", "GO111MODULE": "on", "GOOS": "linux", "GOARCH": "amd64", "GOAMD64": "v1",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "GOMODCACHE": str(work / "cache/modules"), "GOCACHE": str(work / "cache/go"),
        "GOFLAGS": "-mod=readonly -trimpath -buildvcs=false",
        "SOURCE_DATE_EPOCH": str(LOCK["source_date_epoch"]),
        "PATH": str(work / "toolchains/go/bin") + ":/usr/local/bin:/usr/bin:/bin",
    })
    return env


def source_repo(work, name):
    source = LOCK["sources"][name]
    sibling = ROOT.parent / name
    if (sibling / ".git").exists():
        found = subprocess.run(["git", "cat-file", "-e", source["commit"] + "^{commit}"], cwd=sibling, capture_output=True)
        if found.returncode == 0:
            return sibling
    cached = work / "sources" / name
    if not cached.exists():
        cached.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--filter=blob:none", "--no-checkout", source["url"], str(cached)])
    run(["git", "fetch", "origin", source["commit"]], cwd=cached)
    return cached


def refresh_patches(source_parent):
    for name, source in LOCK["sources"].items():
        repo = source_parent / name
        head = run(["git", "rev-parse", "HEAD"], cwd=repo, capture=True).decode().strip()
        check(head == source["commit"], "source checkout does not match locked base: " + name)
        run(["git", "diff", "--exit-code"], cwd=repo, capture=True)
        extra = run(["git", "ls-files", "--others", "--exclude-standard"], cwd=repo, capture=True)
        check(not extra, "stage reviewed source changes before refreshing: " + name)
        patch = run(["git", "diff", "--cached", "--binary", source["commit"]], cwd=repo, capture=True)
        check(patch, "patch is empty: " + name)
        (ROOT / source["patch"]).write_bytes(patch)
        source["patched_tree"] = run(["git", "write-tree"], cwd=repo, capture=True).decode().strip()
        source["patch_sha256"] = digest(patch)
    (ROOT / "sources.lock.json").write_text(json.dumps(LOCK, indent=2) + "\n")
    print("Exported staged patches and source tree hashes; previous artifacts require rebuilding.")


def assemble(work, destination="assembly", patched=True):
    locked_bytes()
    parent = work / destination
    check(not parent.exists(), "assembly exists; choose a fresh --assembly name")
    for name, source in LOCK["sources"].items():
        patch = (ROOT / source["patch"]).read_bytes()
        check(digest(patch) == source["patch_sha256"], "patch checksum mismatch: " + name)
        repo = source_repo(work, name)
        tree = run(["git", "rev-parse", source["commit"] + "^{tree}"], cwd=repo, capture=True).decode().strip()
        check(tree == source["base_tree"], "source tree mismatch: " + name)
        archive = run(["git", "archive", "--format=tar", source["commit"]], cwd=repo, capture=True)
        target = parent / name
        target.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(target, filter="data")
        run(["git", "init", "-q"], cwd=target)
        run(["git", "config", "--local", "core.autocrlf", "false"], cwd=target)
        # Some upstream tracked files match their own .gitignore entries.
        run(["git", "add", "--force", "--all"], cwd=target)
        base = run(["git", "write-tree"], cwd=target, capture=True).decode().strip()
        check(base == source["base_tree"], "extracted source tree mismatch: " + name)
        if patched:
            path = str(ROOT / source["patch"])
            run(["git", "apply", "--check", "--index", path], cwd=target)
            run(["git", "apply", "--index", path], cwd=target)
            tree = run(["git", "write-tree"], cwd=target, capture=True).decode().strip()
            check(tree == source["patched_tree"], "patched source tree mismatch: " + name)
        if name == "coder":
            run(["git", "config", "--local", "core.hooksPath", "scripts/githooks"], cwd=target)
    return parent


def validate_assembly(parent):
    locked_bytes()
    for name, source in LOCK["sources"].items():
        check(digest((ROOT / source["patch"]).read_bytes()) == source["patch_sha256"], "patch queue changed since lock: " + name)
        repo = parent / name
        tree = run(["git", "write-tree"], cwd=repo, capture=True).decode().strip()
        check(tree == source["patched_tree"], "assembly index differs from lock: " + name)
        run(["git", "diff", "--exit-code"], cwd=repo, capture=True)
        extra = run(["git", "ls-files", "--others", "--exclude-standard"], cwd=repo, capture=True)
        check(not extra, "assembly contains untracked files: " + name)


def check_graph(work, parent):
    env = build_env(work)
    args = [str(bootstrap(work)), "list", "-m", "-f", "{{.Path}} {{.Version}}", *LOCK["modules"]]
    selected = dict(line.split() for line in run(args, cwd=parent / "coder", env=env, capture=True).decode().splitlines())
    check(selected == LOCK["modules"], "resolved module graph differs from lock")


def changed_go_files(name):
    patch = (ROOT / LOCK["sources"][name]["patch"]).read_text()
    return sorted(line.removeprefix("+++ b/") for line in patch.splitlines() if line.startswith("+++ b/") and line.endswith(".go"))


def lint(work, parent):
    validate_assembly(parent)
    go = str(bootstrap(work))
    env = build_env(work)
    files = changed_go_files("coder")
    formatted = run([go, "tool", "mvdan.cc/gofumpt", "-d", *files], cwd=parent / "coder", env=env, capture=True)
    imports = run([go, "tool", "github.com/daixiang0/gci", "diff", "-s", "standard", "-s", "default", "-s", "Prefix(github.com/coder,cdr.dev/)", *files], cwd=parent / "coder", env=env, capture=True)
    tailscale_format = run([str(work / "toolchains/go/bin/gofmt"), "-l", *changed_go_files("tailscale")], cwd=parent / "tailscale", capture=True)
    check(not formatted, "changed Coder files need gofumpt formatting")
    check(not imports, "changed Coder imports need gci formatting")
    check(not tailscale_format, "changed Tailscale files need gofmt formatting")
    directory = work / "tools/bin"
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "golangci-lint"
    if not binary.exists():
        env["GOBIN"] = str(directory)
        run([go, "install", LOCK["lint"]["module"] + "@" + LOCK["lint"]["version"]], cwd=parent / "coder", env=env)
    info = run([go, "version", "-m", str(binary)], env=env, capture=True).decode()
    check("github.com/golangci/golangci-lint/v2\t" + LOCK["lint"]["version"] in info, "linter version differs from lock")
    run([str(binary), "run", "--fix=false", "--concurrency=4", "--new-from-patch", str(ROOT / LOCK["sources"]["coder"]["patch"]), "./codersdk/workspacesdk/...", "./cli/...", "./tailnet/..."], cwd=parent / "coder", env=env)
    record = {"lock_sha256": digest(locked_bytes()), "status": "pass", "scope": "changed Go files and new diagnostics in affected Coder packages", "linter": LOCK["lint"], "linter_binary_sha256": digest(binary.read_bytes())}
    evidence = work / "evidence"
    evidence.mkdir(exist_ok=True)
    (evidence / "lint.json").write_bytes(canonical(record))
    print("Passed repository Go formatting and targeted lint checks.", flush=True)


def run_go_tests(work, parent, args, label):
    print("Running " + label + ".", flush=True)
    result = subprocess.run(args[:2] + ["-json"] + args[2:], cwd=parent / "coder", env=build_env(work), capture_output=True)
    packages, failures = [], []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        package = event.get("Package", "")
        if event.get("Action") in ("pass", "fail") and not event.get("Test"):
            packages.append({"package": package, "status": event["Action"]})
        if event.get("Action") == "fail" and event.get("Test"):
            name = event["Test"]
            # Only source-controlled case identifiers, never raw Output events.
            if re.fullmatch(r"Test[A-Za-z0-9_/.-]+", name) and "secret" not in name.lower():
                failures.append(name)
    record = {"label": label, "lock_sha256": digest(locked_bytes()), "source_trees": {name: source["patched_tree"] for name, source in LOCK["sources"].items()}, "status": "pass" if result.returncode == 0 else "fail", "packages": packages, "failed_tests": failures, "diagnostics": safe_diagnostics(result.stderr, parent / "coder") if result.returncode else {}}
    evidence = work / "evidence"
    evidence.mkdir(exist_ok=True)
    (evidence / (label.replace(" ", "-") + ".json")).write_bytes(canonical(record))
    check(result.returncode == 0, "Go " + label + " failed; inspect safe case identifiers in evidence")


def test(work, parent, race=False):
    validate_assembly(parent)
    check_graph(work, parent)
    go = str(bootstrap(work))
    env = build_env(work)
    args = [go, "test", "-count=1", "-timeout=180s"]
    if race:
        args += ["-race", "-run", "^TestProxy"]
    args += ["tailscale.com/derp/...", "tailscale.com/net/tshttpproxy", "./codersdk/workspacesdk"]
    mode = "race" if race else "regression"
    run_go_tests(work, parent, args, "DERP and SDK " + mode)
    if not race:
        cli_args = [go, "test", "-count=1", "-timeout=180s", "-run", "^TestProxy(SSHDisabledAutostart|CLIEnvironmentGuard)$", "./cli"]
        run_go_tests(work, parent, cli_args, "CLI autostart regression")
        run_go_tests(work, parent, [go, "test", "-count=1", "-timeout=180s", "./tailnet/..."], "tailnet regression")
        print("Running Go vet and module verification.", flush=True)
        run([go, "vet", "tailscale.com/derp/...", "tailscale.com/net/tshttpproxy", "./codersdk/workspacesdk", "./cli"], cwd=parent / "coder", env=env)
    run([go, "mod", "verify"], cwd=parent / "coder", env=env)
    print("Passed", "race" if race else "regression, tailnet, CLI, vet and module", "checks.")


def baseline(work, name):
    parent = assemble(work, name, patched=False)
    coder = parent / "coder"
    path = coder / "go.mod"
    original = "replace tailscale.com => " + LOCK["sources"]["tailscale"]["original_replacement"]
    check(original in path.read_text(), "baseline replacement requires review")
    path.write_text(path.read_text().replace(original, "replace tailscale.com => ../tailscale"))
    target = parent / "tailscale/derp/derphttp/websocket_proxy_test.go"
    shutil.copyfile(ROOT / "tests/baseline_test.go.txt", target)
    result = subprocess.run([str(bootstrap(work)), "test", "-run", "TestProxyWebsocket(Routing|GreetingCancellation)$", "-count=1", "-timeout=60s", "tailscale.com/derp/derphttp"], cwd=coder, env=build_env(work), capture_output=True)
    output = result.stdout + result.stderr
    check(result.returncode != 0 and all(label in output for label in (b"unintended origin DNS attempted", b"post-upgrade DERP greeting ignored cancellation")), "baseline did not reproduce both expected failures")
    print("Baseline reproduced proxy omission and post-upgrade cancellation failure.")


def prepare_rollback(work):
    official = LOCK["rollback"]
    output = work / "rollback"
    output.mkdir(exist_ok=True)
    sums = download(official["checksums_url"], official["checksums_sha256"])
    check(any(line.split() == [official["sha256"], official["filename"]] for line in sums.decode().splitlines()), "official publisher checksum differs from lock")
    archive = output / official["filename"]
    if not archive.exists():
        archive.write_bytes(download(official["url"], official["sha256"]))
    check(digest(archive.read_bytes()) == official["sha256"], "official rollback archive checksum mismatch")
    with tarfile.open(archive) as tar:
        member = tar.getmember("./coder")
        check(member.isfile(), "unexpected official CLI archive")
        binary = output / "coder"
        binary.write_bytes(tar.extractfile(member).read())
        binary.chmod(0o755)
    empty = output / "empty-config"
    empty.mkdir(exist_ok=True)
    version = run([str(binary), "--global-config", str(empty), "version"], env={"PATH": "/usr/bin:/bin"}, capture=True).decode()
    check("v" + LOCK["base_version"] in version, "official rollback version mismatch")
    record = {"version": LOCK["base_version"], "archive_sha256": official["sha256"], "binary_sha256": digest(binary.read_bytes()), "publisher_checksums_sha256": official["checksums_sha256"], "verification": "HTTPS publisher checksums and GitHub release asset digest; no signature verification claimed"}
    (output / "rollback.json").write_bytes(canonical(record))
    print("Official rollback CLI retained locally and checksum/version verified; not installed.")


def compile_cli(work, parent, output):
    validate_assembly(parent)
    check_graph(work, parent)
    output.parent.mkdir(parents=True, exist_ok=True)
    run(["bash", "scripts/build_go.sh", "--slim", "--os", "linux", "--arch", "amd64", "--version", LOCK["version"], "--output", str(output)], cwd=parent / "coder", env=build_env(work))
    info = binary_info(work, output)
    empty = work / "empty-cli-config"
    empty.mkdir(exist_ok=True)
    smoke = run([str(output), "--global-config", str(empty), "version"], env={"PATH": "/usr/bin:/bin"}, capture=True).decode()
    check(LOCK["version"] in smoke, "binary version mismatch")
    (output.parent / "module-info.txt").write_text(info)
    for name in LOCK["sources"]:
        directory = output.parent / "licenses" / name
        directory.mkdir(parents=True, exist_ok=True)
        for license_file in (parent / name).glob("LICENSE*"):
            shutil.copyfile(license_file, directory / license_file.name)
    (output.parent / "sources.lock.json").write_bytes(locked_bytes())
    created = datetime.datetime.fromtimestamp(LOCK["source_date_epoch"], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sbom = {"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1, "metadata": {"timestamp": created, "component": {"type": "application", "name": "coder-cli-proxy", "version": LOCK["version"]}}, "components": effective_components(info), "properties": [{"name": "info.proxy-patch.lock.sha256", "value": digest(locked_bytes())}]}
    (output.parent / "sbom.cdx.json").write_bytes(canonical(sbom))
    (output.parent / "recipe.tar.gz").write_bytes(recipe_bytes())
    provenance = expected_provenance(output.parent)
    (output.parent / "provenance.json").write_bytes(canonical(provenance))
    return digest(output.read_bytes())


def binary_info(work, binary):
    info = run([str(bootstrap(work)), "version", "-m", str(binary)], env=build_env(work), capture=True).decode()
    check("../tailscale" in info and all(version in info for version in LOCK["modules"].values()), "binary module information differs from lock")
    return "coder: " + info.split(": ", 1)[1]


def effective_components(info):
    components = []
    for line in info.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0] == "dep":
            component = {"type": "library", "name": fields[1], "version": fields[2]}
            components.append(component)
        elif len(fields) >= 3 and fields[0] == "=>":
            component = components[-1]
            component["properties"] = [{"name": "info.proxy-patch.original-module", "value": component["name"] + "@" + component["version"]}]
            component["name"], component["version"] = fields[1:3]
            if fields[1] == "../tailscale":
                source = LOCK["sources"]["tailscale"]
                component["name"], component["version"] = "github.com/coder/tailscale", source["commit"] + "+" + LOCK["patch_revision"]
                for name in ("commit", "patched_tree", "patch_sha256"):
                    component["properties"].append({"name": "info.proxy-patch." + name, "value": source[name]})
            if len(fields) > 3 and fields[3].startswith("h1:"):
                component.setdefault("properties", []).append({"name": "info.proxy-patch.go-sum", "value": fields[3]})
    return components


def expected_provenance(output):
    return {"version": LOCK["version"], "binary_sha256": digest((output / "coder").read_bytes()), "module_info_sha256": digest((output / "module-info.txt").read_bytes()), "sbom_sha256": digest((output / "sbom.cdx.json").read_bytes()), "recipe_sha256": digest((output / "recipe.tar.gz").read_bytes()), "lock_sha256": digest(locked_bytes()), "sources": LOCK["sources"], "go": LOCK["go"], "packager": LOCK["packager"], "platform": LOCK["platform"], "flags": "-mod=readonly -trimpath -buildvcs=false", "build": ["scripts/build_go.sh", "--slim", "--os=linux", "--arch=amd64"], "source_date_epoch": LOCK["source_date_epoch"]}


def verify_payload(output):
    check((output / "sources.lock.json").read_bytes() == locked_bytes(), "payload lock differs from reviewed lock")
    recorded = json.loads((output / "provenance.json").read_bytes())
    check(recorded == expected_provenance(output), "payload is not bound to current binary, lock and source trees; rebuild required")
    check((output / "recipe.tar.gz").read_bytes() == recipe_bytes(), "build recipe changed; rebuild required")
    sbom = json.loads((output / "sbom.cdx.json").read_bytes())
    check(sbom["components"] == effective_components((output / "module-info.txt").read_text()), "SBOM differs from effective binary modules")


def store_blob(layout, data):
    value = digest(data)
    path = layout / "blobs/sha256" / value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"digest": "sha256:" + value, "size": len(data)}


def add_tar(tar, name, data, mode=0o644):
    item = tarfile.TarInfo(name)
    item.size, item.mode = len(data), mode
    item.uid = item.gid = 65532
    item.mtime = LOCK["source_date_epoch"]
    tar.addfile(item, io.BytesIO(data))


def package_image(work, parent, output):
    validate_assembly(parent)
    verify_payload(output)
    check(binary_info(work, output / "coder") == (output / "module-info.txt").read_text(), "binary module metadata changed since build")
    packager = {"python": platform.python_version(), "zlib": zlib.ZLIB_RUNTIME_VERSION}
    check(packager == LOCK["packager"], "packager versions differ from lock")
    layout = output / "oci"
    check(not layout.exists(), "OCI output already exists")
    layout.mkdir(parents=True)
    runtime = LOCK["runtime"]
    url = "https://" + runtime["registry"] + "/v2/" + runtime["repository"]
    index_bytes = download(url + "/manifests/" + runtime["index_digest"], runtime["index_digest"])
    runtime_index = json.loads(index_bytes)
    check(any(entry["digest"] == runtime["manifest_digest"] and entry.get("platform") == LOCK["platform"] for entry in runtime_index["manifests"]), "runtime platform manifest is absent from locked index")
    (output / "runtime-index.json").write_bytes(index_bytes)
    manifest_bytes = download(url + "/manifests/" + runtime["manifest_digest"], runtime["manifest_digest"])
    (output / "runtime-manifest.json").write_bytes(manifest_bytes)
    manifest = json.loads(manifest_bytes)
    check(manifest.get("schemaVersion") == 2, "unexpected runtime manifest")
    for blob in [manifest["config"], *manifest["layers"]]:
        stored = store_blob(layout, download(url + "/blobs/" + blob["digest"], blob["digest"]))
        check(stored["size"] == blob["size"], "runtime blob size mismatch")
    config_path = layout / "blobs/sha256" / manifest["config"]["digest"].split(":")[1]
    config = json.loads(config_path.read_bytes())
    check(config["os"] == "linux" and config["architecture"] == "amd64", "runtime platform mismatch")
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        add_tar(tar, "usr/local/bin/coder", (output / "coder").read_bytes(), 0o755)
        for license_file in sorted((output / "licenses").rglob("*")):
            if license_file.is_file():
                add_tar(tar, "usr/share/licenses/" + license_file.relative_to(output / "licenses").as_posix(), license_file.read_bytes())
        add_tar(tar, "usr/share/coder-proxy/sources.lock.json", locked_bytes())
    raw_layer = layer.getvalue()
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0, filename="", compresslevel=9) as gz:
        gz.write(raw_layer)
    layer_desc = store_blob(layout, compressed.getvalue())
    layer_desc["mediaType"] = OCI + "layer.v1.tar+gzip"
    created = datetime.datetime.fromtimestamp(LOCK["source_date_epoch"], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    config["created"] = created
    config["config"].update({"User": "65532:65532", "Entrypoint": ["/usr/local/bin/coder"], "Cmd": ["--help"], "WorkingDir": "/"})
    config["config"]["Env"] = [entry for entry in config["config"].get("Env", []) if not entry.startswith("HOME=")] + ["HOME=/home/nonroot"]
    config["config"].pop("ExposedPorts", None)
    labels = config["config"].setdefault("Labels", {})
    labels.update({"org.opencontainers.image.title": "Coder CLI proxy patch", "org.opencontainers.image.version": LOCK["version"], "org.opencontainers.image.revision": LOCK["sources"]["coder"]["commit"], "org.opencontainers.image.base.digest": runtime["manifest_digest"], "info.proxy-patch.lock.sha256": digest(locked_bytes())})
    config["rootfs"]["diff_ids"].append("sha256:" + digest(raw_layer))
    config.setdefault("history", []).append({"created": created, "created_by": "Coder CLI proxy patch, locked local assembly"})
    config_desc = store_blob(layout, canonical(config))
    config_desc["mediaType"] = OCI + "config.v1+json"
    layers = manifest["layers"] + [layer_desc]
    for entry in layers:
        if entry["mediaType"] == "application/vnd.docker.image.rootfs.diff.tar.gzip":
            entry["mediaType"] = OCI + "layer.v1.tar+gzip"
    result = {"schemaVersion": 2, "mediaType": OCI + "manifest.v1+json", "config": config_desc, "layers": layers}
    result_desc = store_blob(layout, canonical(result))
    result_desc.update({"mediaType": OCI + "manifest.v1+json", "platform": LOCK["platform"]})
    registry_index = {"schemaVersion": 2, "mediaType": OCI + "index.v1+json", "manifests": [result_desc]}
    index_desc = store_blob(layout, canonical(registry_index))
    index_desc.update({"mediaType": OCI + "index.v1+json", "annotations": {"org.opencontainers.image.ref.name": LOCK["base_version"] + "-" + LOCK["patch_revision"]}})
    catalog = {"schemaVersion": 2, "mediaType": OCI + "index.v1+json", "manifests": [index_desc]}
    (layout / "index.json").write_bytes(canonical(catalog))
    (layout / "oci-layout").write_bytes(canonical({"imageLayoutVersion": "1.0.0"}))
    archive = output / "coder-cli.oci.tar"
    with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as tar:
        for path in sorted(p for p in layout.rglob("*") if p.is_file()):
            add_tar(tar, path.relative_to(layout).as_posix(), path.read_bytes())
    summary = {"binary_sha256": digest((output / "coder").read_bytes()), "platform_manifest_digest": result_desc["digest"], "registry_index_digest": index_desc["digest"], "layout_catalog_sha256": digest((layout / "index.json").read_bytes()), "archive_sha256": digest(archive.read_bytes())}
    (output / "image-digests.json").write_text(json.dumps(summary, indent=2) + "\n")
    source_archive(parent, output)
    checksums(output)
    verify_image(output)
    return summary


def verify_image(output):
    verify_payload(output)
    verify_checksums(output)
    layout = output / "oci"
    for blob in (layout / "blobs/sha256").iterdir():
        check(digest(blob.read_bytes()) == blob.name, "OCI blob checksum mismatch")
    check(json.loads((layout / "oci-layout").read_bytes()) == {"imageLayoutVersion": "1.0.0"}, "unexpected OCI layout")
    catalog = json.loads((layout / "index.json").read_bytes())
    check(catalog.get("mediaType") == OCI + "index.v1+json" and catalog["schemaVersion"] == 2 and len(catalog["manifests"]) == 1, "unexpected OCI catalog")
    index_descriptor = catalog["manifests"][0]
    check(index_descriptor["mediaType"] == OCI + "index.v1+json", "catalog reference is not a registry index")
    check(index_descriptor.get("annotations") == {"org.opencontainers.image.ref.name": LOCK["base_version"] + "-" + LOCK["patch_revision"]}, "unexpected catalog reference tag")
    index = json.loads(read_descriptor(layout, index_descriptor))
    check(index.get("mediaType") == OCI + "index.v1+json" and index["schemaVersion"] == 2 and len(index["manifests"]) == 1, "unexpected registry image index")
    descriptor = index["manifests"][0]
    check(descriptor["mediaType"] == OCI + "manifest.v1+json", "registry index does not reference a platform manifest")
    check(descriptor["platform"] == LOCK["platform"], "unexpected manifest platform")
    manifest = json.loads(read_descriptor(layout, descriptor))
    check(manifest["schemaVersion"] == 2 and manifest["mediaType"] == OCI + "manifest.v1+json", "unexpected OCI manifest")
    check(manifest["config"]["mediaType"] == OCI + "config.v1+json", "unexpected image configuration type")
    config = json.loads(read_descriptor(layout, manifest["config"]))
    check(config["config"]["Entrypoint"] == ["/usr/local/bin/coder"] and config["config"]["Cmd"] == ["--help"] and config["config"]["User"] == "65532:65532", "unsafe CLI image defaults")
    check([entry for entry in config["config"].get("Env", []) if entry.startswith("HOME=")] == ["HOME=/home/nonroot"], "CLI image home is not explicit")
    check(config["os"] == "linux" and config["architecture"] == "amd64", "unexpected image platform")
    base_bytes = (output / "runtime-manifest.json").read_bytes()
    check("sha256:" + digest(base_bytes) == LOCK["runtime"]["manifest_digest"], "runtime manifest differs from lock")
    base = json.loads(base_bytes)
    base_layers = [{**entry, "mediaType": OCI + "layer.v1.tar+gzip"} for entry in base["layers"]]
    check(manifest["layers"][:-1] == base_layers, "image runtime layers differ from lock")
    runtime_index = (output / "runtime-index.json").read_bytes()
    check("sha256:" + digest(runtime_index) == LOCK["runtime"]["index_digest"], "runtime index differs from lock")
    check(any(entry["digest"] == LOCK["runtime"]["manifest_digest"] and entry.get("platform") == LOCK["platform"] for entry in json.loads(runtime_index)["manifests"]), "runtime manifest platform mismatch")
    raw_layers = []
    for entry in manifest["layers"]:
        check(entry["mediaType"] == OCI + "layer.v1.tar+gzip", "unsupported image layer type")
        raw_layers.append(gzip.decompress(read_descriptor(layout, entry)))
    check(config["rootfs"] == {"type": "layers", "diff_ids": ["sha256:" + digest(raw) for raw in raw_layers]}, "image uncompressed layer checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(raw_layers[-1])) as tar:
        expected = {"usr/local/bin/coder", "usr/share/coder-proxy/sources.lock.json"}
        license_files = [p for p in (output / "licenses").rglob("*") if p.is_file()]
        expected.update("usr/share/licenses/" + p.relative_to(output / "licenses").as_posix() for p in license_files)
        check(set(tar.getnames()) == expected and len(tar.getmembers()) == len(expected), "unexpected CLI layer contents")
        binary = tar.extractfile("usr/local/bin/coder").read()
        check(tar.extractfile("usr/share/coder-proxy/sources.lock.json").read() == locked_bytes(), "image source lock differs")
        for license_file in license_files:
            name = "usr/share/licenses/" + license_file.relative_to(output / "licenses").as_posix()
            check(tar.extractfile(name).read() == license_file.read_bytes(), "image license differs")
        for member in tar:
            check(member.isfile() and member.uid == 65532 and member.gid == 65532 and member.mtime == LOCK["source_date_epoch"], "unexpected CLI layer metadata")
            check(member.mode == (0o755 if member.name == "usr/local/bin/coder" else 0o644), "unexpected CLI layer permissions")
    check(digest(binary) == digest((output / "coder").read_bytes()), "image binary differs from reviewed CLI")
    summary = json.loads((output / "image-digests.json").read_bytes())
    catalog_sha = digest((layout / "index.json").read_bytes())
    check("sha256:" + catalog_sha != index_descriptor["digest"], "layout catalog is not a registry image identity")
    check(summary == {"binary_sha256": digest(binary), "platform_manifest_digest": descriptor["digest"], "registry_index_digest": index_descriptor["digest"], "layout_catalog_sha256": catalog_sha, "archive_sha256": digest((output / "coder-cli.oci.tar").read_bytes())}, "image digest summary differs")
    expected_files = {p.relative_to(layout).as_posix(): p for p in layout.rglob("*") if p.is_file()}
    with tarfile.open(output / "coder-cli.oci.tar") as tar:
        check(set(tar.getnames()) == set(expected_files) and len(tar.getmembers()) == len(expected_files), "OCI archive contents differ from layout")
        for member in tar:
            check(member.isfile() and tar.extractfile(member).read() == expected_files[member.name].read_bytes(), "OCI archive differs from layout")


def read_descriptor(layout, descriptor):
    algorithm, value = descriptor["digest"].split(":", 1)
    check(algorithm == "sha256" and len(value) == 64 and all(c in "0123456789abcdef" for c in value), "invalid OCI digest")
    data = (layout / "blobs/sha256" / value).read_bytes()
    check(len(data) == descriptor["size"] and digest(data) == value, "OCI descriptor size or checksum mismatch")
    return data


def interop(work, parent, output):
    verify_image(output)
    validate_assembly(parent)
    go = str(bootstrap(work))
    directory = work / "tools/oci-validator"
    directory.mkdir(parents=True, exist_ok=True)
    program = directory / "main.go"
    program.write_bytes((ROOT / "tests/oci_validate.go.txt").read_bytes())
    summary = json.loads((output / "image-digests.json").read_bytes())
    scratch = work / "interop"
    scratch.mkdir(exist_ok=True)
    print("Validating and round-tripping the named OCI index with the pinned Go consumer.", flush=True)
    with tempfile.TemporaryDirectory(dir=scratch) as temporary:
        run([go, "run", str(program), str(output / "oci"), summary["registry_index_digest"], summary["platform_manifest_digest"], temporary], cwd=parent / "coder", env=build_env(work))
    version = run([go, "list", "-m", "-f", "{{.Version}}", "github.com/google/go-containerregistry"], cwd=parent / "coder", env=build_env(work), capture=True).decode().strip()
    record = {"status": "pass", "lock_sha256": digest(locked_bytes()), "recipe_sha256": digest((output / "recipe.tar.gz").read_bytes()), "validator": "github.com/google/go-containerregistry", "validator_version": version, "registry_index_digest": summary["registry_index_digest"], "platform_manifest_digest": summary["platform_manifest_digest"], "scope": "local OCI validation and OCI-to-OCI roundtrip; no container execution or publication"}
    evidence = work / "evidence"
    evidence.mkdir(exist_ok=True)
    (evidence / ("OCI-interop-" + output.name + ".json")).write_bytes(canonical(record))
    print("Passed independent OCI consumer validation and digest-preserving local roundtrip.", flush=True)


def source_archive(parent, output):
    path = output / "sources.tar.gz"
    with path.open("wb") as stream, gzip.GzipFile(fileobj=stream, mode="wb", filename="", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(LOCK["sources"]):
            repo = parent / name
            files = run(["git", "ls-files", "-z"], cwd=repo, capture=True).decode().split("\0")
            for relative in sorted(p for p in files if p):
                source = repo / relative
                if source.is_symlink():
                    item = tarfile.TarInfo(name + "/" + relative)
                    item.type = tarfile.SYMTYPE
                    item.linkname = os.readlink(source)
                    item.mode, item.uid, item.gid = 0o777, 65532, 65532
                    item.mtime = LOCK["source_date_epoch"]
                    tar.addfile(item)
                else:
                    add_tar(tar, name + "/" + relative, source.read_bytes(), source.stat().st_mode & 0o777)


def recipe_bytes():
    locked_bytes()
    check(digest(Path(__file__).read_bytes()) == PIPELINE_SHA256, "pipeline source changed during execution")
    stream = io.BytesIO()
    with gzip.GzipFile(fileobj=stream, mode="wb", filename="", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(RECIPE_FILES):
            path = ROOT / name
            check(path.is_file() and not path.is_symlink(), "build recipe file missing")
            add_tar(tar, "proxy-patch/" + name, path.read_bytes(), 0o644)
    return stream.getvalue()


def checksums(output):
    lines = [digest(p.read_bytes()) + "  " + p.name for p in sorted(output.iterdir()) if p.is_file() and p.name != "SHA256SUMS"]
    (output / "SHA256SUMS").write_text("\n".join(lines) + "\n")


def verify_checksums(output):
    expected = [digest(p.read_bytes()) + "  " + p.name for p in sorted(output.iterdir()) if p.is_file() and p.name != "SHA256SUMS"]
    check((output / "SHA256SUMS").read_text() == "\n".join(expected) + "\n", "artifact checksums differ")


def main():
    global EVIDENCE_DIRECTORY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["refresh", "bootstrap", "assemble", "baseline", "rollback", "lint", "test", "race", "build", "image", "interop", "verify", "all"])
    parser.add_argument("--work", type=Path, default=ROOT.parent / ".build")
    parser.add_argument("--assembly", default="assembly")
    parser.add_argument("--dist", default="dist", help="fresh output directory name below --work")
    parser.add_argument("--source-root", type=Path, default=ROOT.parent, help="reviewed source checkouts for refresh")
    args = parser.parse_args()
    work = args.work.resolve()
    EVIDENCE_DIRECTORY = work / "evidence"
    work.mkdir(parents=True, exist_ok=True)
    check(all(Path(name).name == name and name not in ("", ".", "..") for name in (args.assembly, args.dist)), "assembly and dist must be directory names")
    parent = work / args.assembly
    output = work / args.dist
    command = args.command
    if command == "refresh":
        refresh_patches(args.source_root.resolve())
    elif command == "bootstrap":
        bootstrap(work)
    elif command == "assemble":
        assemble(work, args.assembly)
    elif command == "baseline":
        baseline(work, args.assembly)
    elif command == "rollback":
        prepare_rollback(work)
    elif command == "lint":
        lint(work, parent)
    elif command in ("test", "race"):
        test(work, parent, command == "race")
    elif command == "build":
        print("CLI SHA256:", compile_cli(work, parent, output / "coder"))
    elif command == "image":
        summary = package_image(work, parent, output)
        interop(work, parent, output)
        print(json.dumps(summary, indent=2))
    elif command == "interop":
        interop(work, parent, output)
    elif command == "verify":
        verify_image(output)
        print("OCI image and CLI checksums verified.")
    elif command == "all":
        bootstrap(work)
        assemble(work, args.assembly)
        lint(work, parent)
        test(work, parent)
        test(work, parent, race=True)
        compile_cli(work, parent, output / "coder")
        summary = package_image(work, parent, output)
        interop(work, parent, output)
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        # Do not print exception causes or authenticated network URLs.
        print("pipeline stopped:", str(exc) if isinstance(exc, PipelineError) else type(exc).__name__, file=sys.stderr)
        sys.exit(1)
