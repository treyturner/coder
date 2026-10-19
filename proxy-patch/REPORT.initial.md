> Historical October 18 initial candidate, superseded by [REPORT.md](REPORT.md). Digests and qualification statements below describe that earlier payload.

# v2.37.4 proxy patch implementation result

Implementation and local artifact qualification completed on October 18, 2026. **Independent review: PASS for local implementation and artifact readiness, with no remaining blockers in that scope.** The images are unpublished candidates. Subsequent Docker execution checks pass, and the user confirms that the fixed CLI build works.

The CLI version is `2.37.4+proxyfix.1.1d24629`, built from Coder v2.37.4 to match the server version the user reports running. The build retains the exact pinned Tailscale fork and the consuming WebSocket v1.8.14 / x/net v0.58.0 graph. It uses a local module replacement; no dependency upgrade or global module-cache modification is part of the patch.

## Implemented changes

| File | Result |
| --- | --- |
| Tailscale `derp/derphttp/websocket.go` | WebSocket fallback selects the configured proxy, fails closed on selection/authentication errors, supplies silent CONNECT authentication, separates HTTPS-proxy TLS from relay TLS, refuses redirects, sanitizes remote diagnostics, and closes upgraded streams without waiting for an unresponsive close handshake. |
| Tailscale `derp/derphttp/derphttp_client.go` | Post-101 DERP establishment obeys cancellation and the existing ten-second connection cap. Failed streams and CONNECT sockets close correctly; cancellation guards stop and join before successful handoff. Denial diagnostics retain stage/status without response bodies, credentials or authenticated URLs. |
| Tailscale `derp/derphttp/websocket_proxy_test.go` | Isolated local proxy, TLS, denial, authentication, lifetime, policy-selection, direct-dial prevention and real DERP regression fixtures. |
| Coder `go.mod` | Replaces the existing fork replacement with the assembled `../tailscale` checkout. `go.sum` is unchanged. |
| Coder `codersdk/workspacesdk/proxy_integration_test.go` | Local API/coordinator → proxied DERP fallback → WireGuard → SSH integration; the SSH command returns the synthetic identity `fixture-user`. API/session, proxy and gateway credentials stay on their intended hops. |
| Coder `cli/ssh_proxy_test.go` | An actual CLI invocation with disabled autostart rejects a stopped workspace and issues no mutation requests. |
| Coder `tailnet/conn_test.go` | Updates the existing forced-WebSocket assertion to require the status-only diagnostic, consistent with removing the remote denial body. |
| Patch-control repository | Pinned source/patch locks, source assembly, baseline, regression/race/lint/build checks, reproducible CLI/OCI/source/recipe artifacts, independent OCI-consumer roundtrip, CI recipe, rollback preparation and future-update instructions. |

URL-based default CONNECT changes remain outside this queue. The initial DERP 403's origin remains unverified; this patch fixes the WebSocket proxy omission and connection handling, and preserves existing fallback eligibility. It does not establish that a denied deployment route is permitted.

## Executed verification

| Check | Result |
| --- | --- |
| Unpatched exact-pin baseline under Coder v2.37.4 graph | Reproduced both missing proxy routing and ignored post-upgrade DERP cancellation. A ping milestone makes the latter deterministic. |
| `go test tailscale.com/derp/... ./codersdk/workspacesdk` | PASS, full suites. |
| `go test ./tailnet/...` | PASS, full suites with repository-default skip behavior. |
| CLI disabled-autostart fixture | PASS, stopped workspace rejected without start/build calls. |
| `-race -run '^TestProxy'` for DERP/SDK and targeted CLI fixture | PASS. |
| Go vet and `go mod verify` | PASS for affected packages and the consuming graph. |
| Repository Go formatting and affected-package golangci-lint v2.0.2 | PASS, with no automatic fixes or suppressed diagnostics beyond the justified self-exec test annotation. |
| Offline packaging regressions | 14 PASS, including stale payload/lock binding, effective replacements, reproducible archives, descriptor/blob/archive/checksum tampering, distinct catalog/index identity, reference/tag/platform tampering and environment isolation. |
| Independent Go OCI consumer | Both artifacts PASS go-containerregistry v0.20.7 recursive validation and local OCI-to-OCI copy with registry-index and platform digests preserved. |
| Separate source-directory rebuild | All 12 top-level deliverables match byte for byte, including binary, OCI/source/recipe archives, provenance, inventory and checksums. Same Linux amd64 host and locked Go/Python/zlib. |
| Artifact audit | Static ELF64 amd64, CGO disabled, upstream slim/omit tags, expected version and replacement metadata. UID/GID 65532, CLI/help defaults, explicit home with mode 0700, and 150 parsed runtime CA roots. Host CLI help with the image's public environment resolves the intended config path. |
| Docker execution smoke check | PASS on daemon 29.8.2 / client 29.8.0. Loaded the exact frozen OCI archive and ran `version`, the default help command, and `ssh --help`; all exited zero with expected output. Docker's imported index identity, selected platform manifest, configuration and uncompressed layers match the frozen layout. Each container used UID/GID 65532, no networking, a read-only root filesystem, dropped capabilities, no new privileges and only a temporary `/tmp`; no host configuration or credential mounts were supplied. Test containers were removed. |
| User confirmation | On October 18, 2026, the user reported: “the fixed cli build works.” The tested command, stdout, agent version and runtime conditions were not supplied with this confirmation. |
| Secret handling | No local synthetic credential markers in the CLI or artifact metadata. Source archive restricted to 13,583 pinned tracked entries; recipe restricted to 12 reviewed files. Runtime config, home directories, environment dumps and protected bindings are excluded. Public upstream test fixtures remain part of corresponding source. |

Safe execution records are in `../.build/evidence/`. The current source-lock SHA256 is `701cc22a47821f270d86c9410d12deddea100adaae4bb55b7b13f7d662ebeecf`.

The subsequent Docker check is recorded in `../.build/evidence/container-smoke.json`; its rerunnable local driver is `../.build/container-smoke.py` (run with `/usr/bin/python3` after importing the frozen archive). The check used the inherited `DOCKER_HOST` endpoint. The earlier inference that an absent local socket meant no daemon was available was incorrect. These offline CLI checks do not exercise live proxy, DERP or SSH connectivity. The frozen payloads and their checksums are unchanged.

## Frozen artifacts

Primary deliverables are in `../.build/dist/`; the matching rebuild is in `../.build/dist-rebuild/`.

| Identity | Digest |
| --- | --- |
| **Registry image index** | `sha256:dc6e6ed42ec8e5ff0a4bf72da5a43a87a66528cd60a1c554173d2ef7225f2759` |
| Linux amd64 platform manifest | `sha256:51be698a535461b8d1d5b19c88c5fb20c99691862e46b68e0fa6283bed1cd107` |
| Layout catalog SHA256, distinct from registry identity | `9f8150d1294f72d796413b231791a3d710449e401885064ddcf4f5e9f333c996` |
| `coder-cli.oci.tar` SHA256 | `54cf8f7901745fac2532c76e61abe5cb1dd9ecc381a0ded890b82ccbe7a94e73` |
| CLI SHA256 | `491b6f355a2e54f7fbfdd24dffcf7b7b26e2e7d637086e690597cde82708ff6c` |
| Corresponding source SHA256 | `942dde945ce79b6b74681dbdaf9dfb529e4e5a530601e63026ab93c2d1d8d28e` |
| Complete build/patch recipe SHA256 | `261e8df4054a27274ebb023f716b9b53d4ba88038c5783ebe4034ab101425a82` |

The OCI archive is 21,217,280 bytes; the static CLI is 59,203,746 bytes. `SHA256SUMS` covers all top-level payload files. The CycloneDX inventory describes 219 effective linked Go modules and their replacements; it is not a runtime-base inventory or vulnerability assessment.

Coder's final Git tree is `b8b6be8406b9ec5cb69063dde89a7f868bb97dee`; Tailscale's is `8c48de18af867c6b5c0c8c2fc499d1e529184525`. Base commits, original replacement pseudo-version, patch hashes, toolchain checksum and runtime digests are in `sources.lock.json`. Source changes are staged locally; no source commit, PR or remote publication was created.

## Independent review

The independent reviewer performed read-only checks of all 12 matching artifacts, checksums, provenance/lock/recipe bindings, recursively referenced OCI objects, compressed/uncompressed layers and archive agreement. They reconstructed both exact Git trees from the source archive, checked all 219 effective inventory entries against the binary's build metadata, inspected static ELF/build flags and runtime CA/home configuration, and verified the retained official rollback archive corresponds to its extracted binary. All supplied digests matched. The reviewer did not run tests, builds, network operations or containers.

The reviewer approved **local implementation/artifact readiness only**. This verdict does not establish hosted CI, full monorepo hook completion, container execution, live SSH or publication.

## Remaining gates and rollback

- Full monorepo pre-commit was attempted in scratch source. It could not complete because pnpm, shfmt and Terraform were unavailable. Targeted Go formatting/lint, vet and affected suites passed. Hooks remain configured; no commit bypass was used.
- Container CLI execution now passes the offline smoke check above. Hosted CI has not run.
- The user's successful CLI result is recorded above. Agent-performed live acceptance requires the separate approval defined in the reviewed plan, fresh readiness checks, the actual existing agent version and matching fixture coverage if different, the approved relay authority/proxy/CA/protected-auth mechanism, and actual harmless identity output. The current deployment's 403 origin and policy applicability remain unresolved. No agent-initiated live workspace access was attempted.
- Publication requires separate approval of the exact registry index and both destination namespaces/references. Nothing has been pushed to `ghcr.io` or `forgejo.treyturner.info`. Preserve the registry index and platform bytes at both destinations; the layout catalog checksum is not the publication digest.

The official [v2.37.4 release](https://github.com/coder/coder/releases/tag/v2.37.4) Linux amd64 archive and extracted CLI are retained under `../.build/rollback/`, without installation. The locked archive SHA256 and publisher checksum agree; the CLI's version was verified with an empty config. Extracted official CLI SHA256: `29cf7d11333cab252ee9f849d52f31d6e92c78100eb6200314660637d6a95d7a`. No release-signature verification is claimed. Rollback restores the recorded official CLI/client image reference and protected config; it does not promise to remove the official client's proxy defect.

Rebuild/update commands, the regression matrix and approval/stop conditions are documented in [README.md](README.md) and the preserved [approved plan](docs/PLAN.md).
