# Coder CLI proxy patch

This patch queue builds a Coder **v2.37.4** CLI that uses the configured HTTP or HTTPS proxy for DERP WebSocket fallback. It also bounds post-upgrade DERP establishment, closes failed connections, separates HTTPS-proxy TLS from relay TLS, and removes remote bodies and authenticated URLs from connection diagnostics. Connection failures retain fixed TLS verification, DNS, refusal and timeout categories, context sentinel identity, numeric HTTP status and numeric region context.

The server reported running is v2.37.4. Deployment observations from v2.36.7 are historical. Local fixtures establish the routing defect and the patched behavior; they do not establish that the current deployment permits DERP or SSH. The initial DERP 403 has an unverified origin and remains a separate policy question. A proxy CONNECT denial does not authorize direct fallback.

The implementation follows the independently reviewed [implementation plan](docs/PLAN.md), preserved as the approved research record. This repository contains the repeatable build inputs. No runtime credential belongs here.

## Locked inputs

`sources.lock.json` records full commits, original and patched Git tree hashes, patch SHA256s, effective WebSocket and x/net versions, Go archive checksum, packaging versions, platform, official rollback archive, and runtime image digests.

| Input | Selection |
| --- | --- |
| Coder | v2.37.4, `1d246291a8a6ae1948b8c825ee252f6a46bfd561` |
| Coder Tailscale fork | `b7c5fc6e639929414866313e40a587d9c341117d` |
| Effective WebSocket / x/net | v1.8.14 / v0.58.0 |
| Builder | Go 1.26.5, downloaded and checked locally |
| Packager | Python 3.14.4, zlib 1.3.1 |
| Qualified platform | Linux amd64 |
| Runtime | Digest-pinned distroless static, normal system CA trust, UID/GID 65532 |

The primary image is a nonroot CLI image with `/usr/local/bin/coder` as entrypoint, `--help` as its default command, and `HOME=/home/nonroot` for a writable user config location. The pinned base provides that directory with owner 65532 and mode 0700. The upstream slim build uses the enterprise command with its existing license behavior. Both Coder license files and Tailscale's license are included. No server is started by default. Mount existing approved configuration and public CA material at runtime; the image contains no user configuration or runtime authentication. Use an explicit `--global-config` or `CODER_CONFIG_DIR` when the protected binding lives elsewhere.

## Build and verify

Use Linux amd64 with Git, Bash, GNU getopt, a C compiler for race tests, and the locked Python/zlib versions. Go and Go-based tools use task-local directories. No Docker daemon is required for OCI packaging. Keep the existing approved proxy and CA configuration. The pipeline preserves proxy/CA variables, excludes runtime credentials and TLS/debug logging variables from tests and builds, and captures tool output so remote error text cannot enter logs or artifacts.

Run from this repository:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/pipeline.py baseline --assembly baseline
python3 scripts/pipeline.py all --assembly qualified
python3 scripts/pipeline.py rollback
python3 scripts/pipeline.py verify
```

`all` assembles exact public source into sibling `coder` and `tailscale` directories, checks base trees, applies patches with `git apply --check --index`, checks final trees, verifies the consuming module graph, runs formatting/lint and regression/race checks, builds the slim CLI, and packages a local OCI layout and archive. Source assembly uses a local `replace tailscale.com => ../tailscale`; global module caches are never modified. Existing assembly names are refused. Use fresh names for a new run, or a fresh `--work` directory.

The unpatched baseline must reproduce both missing proxy routing and ignored post-upgrade cancellation. The cancellation fixture exchanges a WebSocket ping first so it reaches the stalled DERP greeting deterministically. Baseline failure is expected; the pipeline succeeds only when both regression labels are present.

The pipeline runs full DERP and workspace SDK suites, full tailnet suites, targeted CLI autostart coverage once per full qualification, Go vet, module verification, and race checks for DERP and SDK `TestProxy` cases. The CLI autostart fixture is repeated for each new base without compiling a second CLI test binary under the race detector. It uses Coder's consuming dependency graph rather than the fork's standalone graph. The formatter and linter versions come from Coder's declared tools and locked golangci-lint v2.0.2. Safe per-package results are saved below the work directory's `evidence/`; raw HTTP/test output and environment dumps are not saved. Failures stop the pipeline. Investigate using reviewed local fixtures without enabling credential/body/key logging.

The upstream repository's broader pre-commit hook also covers unrelated frontend, Terraform, shell formatting and generators. Run it in a fully provisioned development checkout before a source contribution or release that requires that gate. A local inability to run this gate must be recorded explicitly; targeted Go checks do not imply that it passed. Hooks remain configured and are never bypassed for commits.

The default work directory is `../.build`. Deliverables below `dist/` include:

- `coder`, `module-info.txt`, `provenance.json`, and an effective-module CycloneDX inventory.
- `oci/`, `coder-cli.oci.tar`, and `image-digests.json`.
- Patched tracked source in `sources.tar.gz`, the complete build/patch recipe in `recipe.tar.gz`, source locks, license notices, and `SHA256SUMS`.

The CycloneDX inventory describes linked Go modules and effective replacements; it is not a vulnerability assessment or a complete inventory of runtime-base packages. Local patched Tailscale is identified by base commit, patched tree, and patch checksum. Original module identities are retained as properties. Runtime base identity is recorded separately.

Packaging verifies the binary's build metadata against its recorded metadata and binds provenance to the binary, current lock, exact source trees, and inventory. Changed locks or payloads require rebuilding. Verification checks all artifact checksums, OCI descriptor sizes/digests, compressed and uncompressed layer digests, the embedded binary/lock/licenses, image defaults/platform, and archive/layout agreement. The layout catalog names a stored single-platform registry index; `image-digests.json` distinguishes its `registry_index_digest`, `platform_manifest_digest`, and `layout_catalog_sha256`. The catalog's checksum is not a registry image identity. This structure follows the [OCI layout specification](https://github.com/opencontainers/image-spec/blob/v1.1.1/image-layout.md#indexjson-file).

The `image` and `all` commands also validate and round-trip the named index through the independent Go OCI consumer pinned by Coder's module graph (currently go-containerregistry v0.20.7), checking that registry and platform digests survive a local OCI-to-OCI copy. `interop` repeats this check; `verify` performs the Python checks offline without requiring Go. These checks detect accidental drift; review of the recorded digests provides the release identity. No signing identity is created.

To check reproduction from another source directory:

```sh
python3 scripts/pipeline.py assemble --assembly rebuild
python3 scripts/pipeline.py build --assembly rebuild --dist dist-rebuild
python3 scripts/pipeline.py image --assembly rebuild --dist dist-rebuild
cmp ../.build/dist/coder ../.build/dist-rebuild/coder
cmp ../.build/dist/coder-cli.oci.tar ../.build/dist-rebuild/coder-cli.oci.tar
cmp ../.build/dist/sources.tar.gz ../.build/dist-rebuild/sources.tar.gz
```

The pipeline pins byte-sensitive packaging versions and fixes source timestamps, file order, owners, permissions, and compression settings. A repeat on one host demonstrates that configuration's reproduction; it is not evidence that every operating system or toolchain build yields identical bytes. The CI workflow is a recipe for the same checks, with pinned actions and no publication or live SSH step. Local execution does not claim that hosted CI has run.

`Dockerfile` is an alternative recipe for the same reviewed binary and notices. Build it with `dist/` as context after verification. Its builder may produce different image metadata or compression; review and record those new digests separately. The daemonless OCI archive is the primary artifact to mirror unchanged.

## Regression coverage

| Area | Local coverage |
| --- | --- |
| Selection | HTTP_PROXY, HTTPS_PROXY, lowercase variants, unequal nonempty case variants rejected before CLI API and DERP traffic, representative NO_PROXY bypass and ws/wss normalization, CGI error, unchanged ALL_PROXY behavior, unsupported schemes fail closed |
| Routing | HTTP CONNECT, HTTPS proxy, plain HTTP absolute-form upgrade, permitted direct and NO_PROXY traffic, zero unintended origin DNS, guarded IPv4 dialing |
| TLS/auth | Different proxy and relay roots/names, verified chains, relay-only client certificate, synthetic proxy Basic auth, silent auth callback errors, API session and inner gateway headers kept on their intended hops |
| Denials | CONNECT 403/407, inner 403, malformed 101, wrong subprotocol, redirects rejected without reaching the target, synthetic remote secret markers absent from diagnostics |
| Lifetime | Proxy dial/TLS/CONNECT, relay TLS, upgrade, stalled DERP greeting, invalid greeting, explicit close, earlier cancellation, default ten-second cap, peer socket closure, successful connection survives dial-context cancellation |
| Protocol | Initial DERP, forced WebSocket, 403-triggered fallback, real two-client DERP packet exchange |
| Coder | Local API/coordinator, blocked direct endpoints, relay fallback, WireGuard and SSH returning a synthetic identity; CLI refuses stopped workspace with disabled autostart and no mutation calls |
| Packaging | Stale binary/lock rejection, effective replacement metadata, deterministic archives, blob/archive/checksum/descriptor tampering detection, isolated build environment |

Go's API selector prefers uppercase proxy variables in this toolchain; the pinned Tailscale selector prefers lowercase when both are set. The patch preserves those selectors. Do not silently rewrite deployment environment variables. Region WebSocket URLs may use a populated IPv4 while initial selection uses HostName, and the initial selector omits the node port. Local tests characterize this inherited behavior; inspect the approved live target's effective authority and stop on a policy mismatch.

Transport selection, routing, denied upgrades and pre-upgrade cancellation use per-call proxy injection and guarded dialing in parallel. Real environment selection, system-root boundaries and full client establishment retain subprocess isolation. The default ten-second deadline is exercised once in plain qualification; race qualification covers explicit cancellation without waiting out that deadline again. Dependency matcher internals are outside this focused matrix.

## Update a future revision

Generate a draft with `python3 scripts/pipeline.py bump --coder-tag v2.37.5 --work /absolute/task/build`. The helper derives official source commits/trees, the pinned Tailscale replacement, effective dependency versions, a checked compatible Go archive, rollback URLs/checksums and the source epoch. It writes `sources.next.json`, with patched tree and patch hashes deliberately unset, and leaves the reviewed lock and publication approvals untouched. Choose a fresh `--draft-lock` path when retaining multiple drafts.

1. Search current official Coder/Tailscale releases, issue 26544, and candidate fixes again. Prefer a validated official fix when available. Do not carry a redundant routing patch forward automatically.
2. Review the generated draft and adopt it as the source lock. Review runtime digests, choose `patch_revision`, and update the derived version. Image tags and patched dependency metadata derive from `patch_revision`; the CLI version must match `<base>+<patch_revision>.<commit-prefix>`. Keep additional architectures unqualified until their own checks pass.
3. Clone clean source below the workspace, check out those exact commits, and apply/rebase only the relevant changes. Changed context, proxy policy, TLS/auth code, or dependency behavior requires review. Do not accept a fuzzy or conflict-resolved patch without reviewing it.
4. Format and stage only the intended source changes in each checkout. Run `python3 scripts/pipeline.py refresh --source-root <source-parent>`. It checks HEAD against the lock, requires all working changes staged, exports binary-safe patches, and updates patch/tree hashes. It does not commit or publish.
5. Run the baseline, complete matrix, build, reproduction, and independent code/artifact review against the new lock. Keep the previous verified artifact and its official counterpart. The workflow performs no automatic live tests or publishing.

Do not edit module-cache contents or replace the entire Tailscale fork with a newer branch to make a patch apply. URL-based default CONNECT routing is outside this queue; Coder's region path already has CONNECT. Custom URL dialers retain their existing precedence.

## Live acceptance and publication

Implementation approval covers hermetic tests and local builds. **Live acceptance and publication require separate approvals** under the reviewed plan. Registry namespaces/image names and the existing agent version remain to be established. No server update, firewall change, access expansion, credential creation, or remote workload is authorized by this repository.

For live acceptance, approval must identify the reviewed CLI/image digest, existing workspace/agent, protected authentication binding, time window and sole command. First confirm through approved read-only API access that the server is v2.37.4 and the workspace is already running with a healthy agent. Record its actual agent version and qualify a matching local fixture if different. Preserve proxy/CA policy and inspect the effective relay authority. Disable direct peer connections, autostart, forwarding, TTY/session injection, and debug capture. Use a finite timeout and the reviewed flag shape:

```text
<approved-coder> --disable-direct-connections ssh --disable-autostart --wait=no <existing-workspace> -- id -un
```

Require actual expected SSH stdout and exit status zero. HTTP 101, DERP packets, or successful API requests alone are insufficient. Stop on policy denial, TLS/auth failure, unexpected authority/redirect, unintended direct dialing, timeout, unverified readiness/version support, or missing output. Do not alter settings or extract substitute credentials to make it pass.

For publication, approve the exact reviewed OCI index/platform digests and both destination references, for example `ghcr.io/<namespace>/coder-cli-proxy:2.37.4-proxyfix.1` and `forgejo.treyturner.info/<namespace>/coder-cli-proxy:2.37.4-proxyfix.1`. Use an existing protected credential helper/auth file and a reviewed registry-copy tool capable of preserving digests. Copy the same OCI layout to both destinations, preserving manifest bytes and compression, then independently compare index and platform manifest digests and the embedded CLI hash. Stop if TLS/authentication is unavailable or a registry rewrites required bytes. Do not build separately for each registry, create credentials, put credentials in arguments or build layers, overwrite official tags, or update rolling aliases. Publish source/patches/recipe, notices, inventory, provenance and checksums alongside the approved image.

The local OCI layout can be imported with an OCI-aware tool for approved hermetic smoke checks when a local runtime is available. Publication remains separately gated. Static layout verification and host CLI execution do not demonstrate container execution. Record any unavailable container-runtime gate explicitly.

## Rollback

`pipeline.py rollback` retains the official v2.37.4 Linux amd64 release archive and binary below `../.build/rollback`, checks its locked GitHub digest against the publisher checksum file, and verifies its version with an empty config. It does not install or overwrite a CLI. This is checksum/HTTPS verification; no release-signature verification is claimed.

Restore the previously recorded official CLI path, or the last verified v2.37.4 image digest and any explicitly changed client launcher reference. Preserve the existing protected config. A rollback restores official baseline behavior; it does not promise that the original proxy failure will disappear. The already completed server update is outside this client rollback.
