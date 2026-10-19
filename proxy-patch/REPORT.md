# v2.37.4 proxy patch review qualification

The October 19, 2026 review candidate passes local source qualification, race checks, reproducibility checks and offline Docker execution. Independent source/control and artifact review passes. This report supersedes [the initial candidate report](REPORT.initial.md); the earlier user confirmation applies to that initial CLI.

The version remains `2.37.4+proxyfix.1.1d24629`, based on official Coder v2.37.4 and its exact pinned Tailscale fork. The source changes add proxy routing for WebSocket fallback, retain verified relay/proxy TLS and hop-specific authentication, bound connection establishment and cleanup, and provide safe stage/status/cause diagnostics. Conflicting nonempty uppercase/lowercase HTTP_PROXY, HTTPS_PROXY or NO_PROXY pairs stop before CLI networking and both DERP connection paths; values are never printed. Tests inject proxy selection where possible and retain isolated real-environment integration cases.

Review fixes also simplify formatter input and CI, disable inherited automation through repository settings, make matching release retries depend on payload identity, avoid anonymous API downloads and publication-time recompression, isolate the registry publisher, and derive future source-lock drafts from official tags. Each of the 14 review comments has its own commit. The source-lock and patch checksums agree at every commit.

## Executed verification

| Check | Result |
| --- | --- |
| Exact unpatched v2.37.4 baseline | Reproduced proxy omission and ignored cancellation after upgrade. |
| Full DERP, tshttpproxy and SDK suites | PASS, including local coordinator, proxied DERP, WireGuard and SSH fixture output. |
| CLI and tailnet | PASS, including stopped-workspace autostart prevention and conflicting proxy environment guard. CLI qualification runs once per candidate. |
| Race checks | PASS for patch regressions in DERP, tshttpproxy and SDK. |
| Static checks | PASS for repository formatting, affected-package golangci-lint v2.0.2, Go vet and module verification. |
| Build/publication control | 51 Python checks PASS; active-workflow actionlint PASS. |
| Standalone publisher | 12 TLS/registry/race cases PASS; HTTPS-only destination flags and stale-source isolation covered. |
| Release retention | All 12 actual candidate payload files pass offline interrupted-upload, cross-commit resume and completed-retry fixtures. |
| Future bump | Official v2.37.4 metadata reproduces every derived lock field; patch hashes/trees remain unset for manual rebasing and review. |
| Separate source rebuild | All 12 top-level files match byte for byte from separate source directories, with the same Linux amd64 host and locked toolchain/packager. |
| OCI interoperability | Independent Go consumer validates and round-trips both copies without changing registry index or platform digests. |
| Offline Docker execution | Version, default help and SSH help pass using the exact OCI archive, UID/GID 65532, no networking, read-only filesystem, dropped capabilities and no host/config/credential mounts. Client 29.9.1; daemon 29.8.2. |
| GitHub workflow settings | Verified through the existing authenticated CLI: inherited workflows disabled; registered patch workflows enabled. |

The complete local qualification used `review-qualified-v3` and `review-rebuild`, with payloads under the task build directory's `review-dist/` and `review-dist-rebuild/`. Safe records are under `evidence/`, including `review-reproduction.json`, `review-container-smoke.json`, `review-companion-payload.json` and `review-bump.json`. Hosted qualification is tracked on PR #1 and must pass for its final head; previous hosted runs describe the initial candidate.

## Current candidate identities

| Identity | SHA256 |
| --- | --- |
| Registry image index | `sha256:255abe17668b0c568f6e42ea4430dd29828fa414a3a08ab36751e8ba397a99bc` |
| Linux amd64 manifest | `sha256:6b9c4771ec6d7c7e57d1f40829ff274bbf601fe582cea0be6c2c419afe07dfba` |
| CLI | `31950ade2a62c18c60c226c7e00a378fd2adf421c4d0b44f60cfee248240cb6b` |
| OCI archive | `453db195d22112ef6c82910146cead255f369aa5dc523dac4ae5b4ffe134e599` |
| Corresponding source archive | `96e582dba83be9ec881ac37206a3180f8d446ce2528fe0ec9be3fe6ec46b4da4` |
| Frozen build recipe | `cd3916af61a3d86569679930a8ffb463c1a50d0f01e750da610af45f7b7d850e` |
| Source lock | `010750cb3a8f43e06a88ad0200605364d60b24e13cd3c28c06094ac54cf361ed` |

Coder's patched tree is `15464ec1d1f034aa801384b5d2d31b749c113aef`; Tailscale's is `35fa9cea31cb77ed187130dc8e584152f779b15e`. The independent reviewer reconstructed both source trees from all 13,587 source-archive entries and verified the 13-file recipe, provenance, 219-component inventory, binary metadata and OCI descriptors/layers. `SHA256SUMS` covers all 12 top-level deliverables. The exact source commits, patch hashes, selected dependencies, Go/toolchain checksums and runtime image are pinned in `sources.lock.json`. No global module cache is modified.

The candidate is unpublished. Merge and publication retain the existing separate gates; publication requires successful verification of the current main commit, matching reviewed digests, durable release assets and digest readback at both configured registries. The current server's initial DERP 403 origin remains unverified. No live workspace test or server/network change was performed for these review revisions. The retained, checksummed official v2.37.4 CLI provides the documented rollback; it is not installed automatically.
