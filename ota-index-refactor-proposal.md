# Proposal: Publish Firmware Binaries as OCI Artifacts on GitHub Container Registry

First of all — a huge thank you to everyone who has contributed to this project. Being able to keep using existing wall switches to control smart lighting — without rewiring, without replacing hardware, and with full Z2M/ZHA integration — is genuinely impressive work and has added enormous value to a lot of home automation setups, including mine. I want to help make the project even better and easier to contribute to, which is the motivation behind this proposal.

## Problem Statement
Currently, firmware binaries are committed and stored directly in the repository. This approach has several obvious drawbacks:

- **Repository Bloat:** Every firmware rebuild adds thousands of binary files to git history, permanently inflating the repository for everyone who clones it.
  - Over **25,000 binary files** ever committed under `bin/` in git history.
    <details>
      <summary>Command used</summary>
      <pre>git rev-list --objects --all | grep bin/ | wc -l</pre>
    </details>
  - The current `bin/` directory alone uses **144 MB** of disk space; the `.git` folder is **137 MB**.
    <details>
      <summary>Commands used</summary>
      <pre>du -sh bin/ .git</pre>
    </details>
- **Merge Conflicts:** Binary files are not merge-friendly, leading to frequent and hard-to-resolve conflicts.
- **Misuse of VCS:** Git is designed for source code and text, not for large, frequently changing binary blobs.

## Proposed Solution

Stop committing firmware binaries to the repository. Instead, use [ORAS](https://oras.land/) (OCI Registry As Storage) to push firmware as artifacts to **GitHub Container Registry** (`ghcr.io`) on each tagged release. ORAS is a small CLI tool (also available as a GitHub Action) that lets you push and pull arbitrary files to/from an OCI registry, without needing Docker or containers.

This is not a novel approach — it is exactly how **Homebrew** distributes its prebuilt packages: each formula's bottle is stored as an OCI artifact on `ghcr.io`, with 9,000+ packages and no limits.

- **Every build** uploads a single zip of all firmware files (`.zigbee`, `.bin`, `.s37`) as a GitHub Actions artifact (retained 90 days). This is intended for development and testing: contributors can download the zip, extract the file for their device, and flash it directly.
- **On tagged release**, the `.zigbee` files are pushed as OCI artifacts to `ghcr.io`. One package per board, one tag per role+variant. For example, `MODULE_AVATTO_TS0011` would get a package at `ghcr.io/OWNER/tuya-zigbee-switch/MODULE_AVATTO_TS0011` with tags like:
  - `v1.2.3-router`, `v1.2.3-router-forced`, `v1.2.3-router-from_tuya`
  - `v1.2.3-end_device`, `v1.2.3-end_device-forced`, `v1.2.3-end_device-from_tuya`

  All tags are visible on the package page. Packages are public, free, and retained forever.
- **The OTA index** is updated and committed to the repo. It contains direct HTTPS blob URLs pointing to the `.zigbee` artifacts on `ghcr.io`, which Z2M/ZHA use to push firmware over-the-air. No authentication or special tooling required.

## Implementation Notes

<details>
<summary>Package and tag structure</summary>

Each board gets one OCI package (e.g. `MODULE_AVATTO_TS0011`). Only `.zigbee` files are pushed — one tag per role+variant combination:

```sh
oras push ghcr.io/OWNER/tuya-zigbee-switch/MODULE_AVATTO_TS0011:v1.2.3-router \
  bin/router/MODULE_AVATTO_TS0011/tlc_switch-v1.2.3.zigbee

oras push ghcr.io/OWNER/tuya-zigbee-switch/MODULE_AVATTO_TS0011:v1.2.3-router-forced \
  bin/router/MODULE_AVATTO_TS0011/tlc_switch-v1.2.3-forced.zigbee

oras push ghcr.io/OWNER/tuya-zigbee-switch/MODULE_AVATTO_TS0011:v1.2.3-router-from_tuya \
  bin/router/MODULE_AVATTO_TS0011/tlc_switch-v1.2.3-from_tuya.zigbee

oras push ghcr.io/OWNER/tuya-zigbee-switch/MODULE_AVATTO_TS0011:v1.2.3-end_device \
  bin/end_device/MODULE_AVATTO_TS0011_END_DEVICE/tlc_switch-v1.2.3.zigbee

oras push ghcr.io/OWNER/tuya-zigbee-switch/MODULE_AVATTO_TS0011:v1.2.3-end_device-forced \
  bin/end_device/MODULE_AVATTO_TS0011_END_DEVICE/tlc_switch-v1.2.3-forced.zigbee

oras push ghcr.io/OWNER/tuya-zigbee-switch/MODULE_AVATTO_TS0011:v1.2.3-end_device-from_tuya \
  bin/end_device/MODULE_AVATTO_TS0011_END_DEVICE/tlc_switch-v1.2.3-from_tuya.zigbee
```

Other file formats (`.bin`, `.s37`) are only available via the GitHub Actions artifact zip, which is sufficient for the direct-flashing use case (development/testing). Restricting OCI to `.zigbee` keeps the package clean and avoids UX confusion — users cannot easily identify or download individual layers from the GitHub packages UI.

Pushing uses `GITHUB_TOKEN` in the CI workflow — no extra secrets needed.
</details>

<details>
<summary>OTA index URL construction</summary>

`oras push` outputs the digest of each pushed layer, so the CI script captures the `.zigbee` digest at push time and constructs the blob URL directly:

```
https://ghcr.io/v2/OWNER/tuya-zigbee-switch/MODULE_AVATTO_TS0011/blobs/sha256:DIGEST
```

The human-readable identity (board name, variant, version) is carried by the `fileName` field in the OTA index alongside the `url` — consumers like Z2M never need to parse the URL itself.

Note: OCI blob URLs are always content-addressed (`blobs/sha256:DIGEST`) — this is fundamental to the storage model and unaffected by package or tag naming. Tags appear on the package page for easy browsing but resolve to a manifest, not the file directly.
</details>

## Benefits
- **Lean repository:** No more binary blobs in git history; future clones will be orders of magnitude smaller.
- **Scales without limits:** With one package per board (~130 boards today), we are well within any practical limit — and can add boards indefinitely.
- **Free and permanent:** Public packages on `ghcr.io` are free and are not auto-deleted.
- **Traceability:** Each firmware is versioned, tagged, and content-addressable. The OTA index always points to the exact binary built for a given release.
- **No special tooling for users:** Firmware blobs are downloadable via plain HTTPS — no OCI client needed by end users or Z2M.

## Request for Feedback
Would maintainers and contributors support this change? Are there use cases that require binaries to be present in the repo? Any concerns about using `ghcr.io` as the firmware store?

## Alternatives Considered

### GitHub Release Assets
Publishing firmware as GitHub Release assets was the first alternative considered. It requires no extra tooling and is easy to understand. However, GitHub limits the number of assets per release to **1,000**, and we are already at ~660 firmware files per release (router + end_device variants for all boards). This will not scale as support for more devices is added.

## Annex: What Is an OCI Registry?

An **OCI Registry** is a standardized server for storing and distributing software artifacts.
You may know it as the technology behind Docker images, but modern OCI registries support storing
**any file** — not just container images. Think of it as a content-addressable, versioned file store
with a well-known HTTP API.

**GitHub Container Registry** (`ghcr.io`) is GitHub's OCI registry. It is:
- Free for public repositories.
- Unlimited in the number of packages and versions.
- Retained forever (packages are not auto-deleted).
- Accessible over plain HTTPS without any special tooling (for public packages).

This is exactly how **Homebrew** stores its prebuilt binary packages ("bottles"):
each formula's bottle is stored as a separate OCI package on `ghcr.io`
(e.g., `ghcr.io/homebrew/core/sqlite`, `ghcr.io/homebrew/core/openssl`, etc.),
with thousands of packages and no limits.
