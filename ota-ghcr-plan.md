# Implementation Plan: Publish Firmware Binaries to ghcr.io

## Project Context

`tuya-zigbee-switch` is an open-source Zigbee firmware for Tuya wall switches. It replaces the stock Tuya firmware with a build compatible with Zigbee2MQTT (Z2M) and ZHA. The repo is at `github.com/romasku/tuya-zigbee-switch`.

Each supported device ("board") is defined in `device_db.yaml`. The build system reads that file to compile per-board firmware and generate Zigbee OTA upgrade files (`.zigbee`). Z2M polls an OTA index JSON file (`zigbee2mqtt/ota/index_router.json` and `index_end_device.json`) to know what firmware is available and where to download it from.

Currently, firmware binaries are committed to git under `bin/`. This inflates the repo and causes merge conflicts. The goal is to stop committing them, push `.zigbee` files to `ghcr.io` on tagged releases, and have the OTA index point to the ghcr.io blob URLs.

---

## Relevant Files and Their Current State

### `device_db.yaml`

YAML file keyed by board name. Each entry has:
```yaml
BOARD__MHCOZY_TS0004:
  device_type: router          # "router" or "end_device"
  mcu_family: Telink           # "Telink" or "Silabs"
  mcu: TLSR8258
  config_str: "imaccztn;TS0004-MC;..."
  stock_manufacturer_name: _TZ3000_imaccztn
  stock_manufacturer_id: 4417
  stock_image_type: 54179
  firmware_image_type: 43551
  build: yes                   # "yes" or "no"
  # ...
```

`device_type: end_device` means the board is built for both router and end_device roles. `device_type: router` means router only.

### `board.mk`

The per-board build Makefile. Called as `BOARD=NAME DEVICE_TYPE=router make board/build`. Key variables derived from `device_db.yaml` via `yq`:

```makefile
BIN_PATH        := bin/$(DEVICE_TYPE)/$(BOARD_DIR)   # e.g. bin/router/BOARD__MHCOZY_TS0004
OTA_FILE        := $(BIN_PATH)/tlc_switch-$(VERSION_STR).zigbee
FROM_TUYA_OTA_FILE := $(BIN_PATH)/tlc_switch-$(VERSION_STR)-from_tuya.zigbee
FORCE_OTA_FILE  := $(BIN_PATH)/tlc_switch-$(VERSION_STR)-forced.zigbee
Z2M_INDEX_FILE  := zigbee2mqtt/ota/index_$(DEVICE_TYPE).json
Z2M_FORCE_INDEX_FILE := zigbee2mqtt/ota/index_$(DEVICE_TYPE)-FORCE.json
```

The `build` target runs: `drop-old-files → build-firmware → generate-ota-files → update-indexes`

`update-indexes` calls `make_z2m_ota_index.py` up to 3 times (once per `.zigbee` file).

**Current committed state**: No `OTA_BASE_URL` variable exists. The script is called without `--base-url`, so the script falls back to `get_raw_github_link()` which detects the branch from git and produces a `raw.refs/heads/...` URL pointing into the git tree.

```makefile
update-indexes:
    @python3 $(HELPERS_PATH)/make_z2m_ota_index.py --db_file $(DEVICE_DB_FILE) $(OTA_FILE) $(Z2M_INDEX_FILE) --board $(BOARD)
    # ... also for from_tuya and forced variants (no --base-url passed)
```

### `helper_scripts/make_z2m_ota_index.py`

Called once per `.zigbee` file. Arguments: `INPUT_FILE`, `INDEX_JSON_FILE`, `--board BOARD`, `--db_file YAML`, `--base-url URL`.

Reads the Zigbee OTA header bytes from the binary to extract `fileVersion`, `imageType`, `manufacturerCode`, etc.

**Current committed state**: `--base-url` is `required=False` with a default computed at import time by `get_raw_github_link()`, which runs `git branch`, `git config`, and `git remote get-url` to construct a `raw.refs/heads/{branch}` URL. The function and `import subprocess` are present. The `make_ota_index_entry()` function signature is `(file, base_url, ...)` and builds `fileName` and `url` from `file` and `base_url` directly (not using a `BOARD--filename` prefix).

The URL it currently produces looks like:
```
https://github.com/romasku/tuya-zigbee-switch/raw/refs/heads/main/bin/router/BOARD__MHCOZY_TS0004/tlc_switch-1.1.2-abc1234.zigbee
```
This points directly into the git tree, which only works because binaries are currently committed.

Then upserts the entry into the index JSON (replacing any entry with the same `manufacturerCode` + `imageType` + `manufacturerName`).

### OTA index entry format

```json
{
  "fileName": "tlc_switch-1.1.2-abc1234.zigbee",
  "fileVersion": 285356032,
  "fileSize": 176162,
  "url": "https://...",
  "imageType": 43551,
  "manufacturerCode": 4417,
  "sha512": "7ac04d...",
  "otaHeaderString": "Telink OTA Image\u0000...",
  "manufacturerName": ["_TZ3000_imaccztn", "imaccztn"]
}
```

Z2M uses `manufacturerCode`, `imageType`, and `manufacturerName` to match a device to an OTA entry, then fetches the file from `url`. The `url` must be a direct HTTPS link that returns the raw binary (no authentication, no redirects to HTML).

### `.github/workflows/build.yml`

**Current committed state**: Triggered on `workflow_dispatch` only (no automatic push or tag trigger). Uses old action versions (`actions/checkout@v2`, `actions/cache@v3`). No `permissions` key. Builds all boards unconditionally (no tag detection). The commit step runs:

```bash
git add bin/* zigbee2mqtt/* zha/* homed/* docs/supported_devices.md
git commit -m "Update firmware files, converters, quirks..."
make tools/freeze_ota_links
git add zigbee2mqtt/*
git commit -m "Freeze links in OTA index files"
git push
```

So binaries are committed on every build, OTA index URLs point into the git tree (raw branch refs), and links are then frozen to a commit hash. There is no GH Release creation and no Actions artifact upload.

### `.gitignore`

**Current committed state**: Does **not** exclude `bin/router/` or `bin/end_device/`. Those directories are committed to git on every build.

---

## Goal

On tagged releases:
1. Push each `.zigbee` file to `ghcr.io` as an OCI artifact (one package per board, one tag per role+variant).
2. Construct the OCI blob URL for each file and write it into the OTA index.
3. Commit the updated OTA index. Do **not** commit firmware binaries.
4. Create a GH Release (changelogs/visibility only), with **no** firmware assets attached.

---

## Changes Required

### 1. `.gitignore`

Add:
```
bin/router/
bin/end_device/
```

`bin/_factory/` (stock firmware dumps for reference) is kept.

### 2. `helper_scripts/make_z2m_ota_index.py`

The current file uses `get_raw_github_link()` (which shells out to git) to auto-detect the URL. This must be replaced. The new logic:

1. Remove `import subprocess` and the entire `get_raw_github_link()` function.
2. Keep `fileName` as the original filename (no prefix). With OCI, each board is its own package, so there is no filename collision across boards.
3. Replace `--base-url required=False, default=get_raw_github_link()` with a single required `--url` argument:
   ```python
   parser.add_argument("--url", required=True, help="Direct download URL for this file")
   ```
4. Update the URL/filename construction accordingly:
   ```python
   filename = asset_path.name  # e.g. tlc_switch-1.2.0-abc1234.zigbee
   entry["fileName"] = filename
   entry["url"] = args.url
   ```

`--base-url` is not needed: the only caller is the CI workflow (via `prepare_release.py`, step 4), which always has an exact URL available (the OCI blob URL computed from `sha256sum`). The `update-indexes` target in `board.mk` is removed entirely (step 3).

### 3. `board.mk`

Since `--url` is now required, the existing `update-indexes` target would fail without it. Remove `update-indexes` from the `build` target's dependency list:

```makefile
# Before:
build: drop-old-files build-firmware generate-ota-files update-indexes

# After:
build: drop-old-files build-firmware generate-ota-files
```

Also remove the `update-indexes` target definition entirely — it's dead code. The CI workflow calls `make_z2m_ota_index.py` directly (step 4), not through Make.

No other changes to `board.mk` are needed.

### 4. `helper_scripts/prepare_release.py` (new file)

A pure-preparation script with **no side effects** (no network calls, no git operations). It:

1. Cleans all OTA index files (equivalent to `make tools/clean_z2m_index`)
2. Finds all `.zigbee` files under `bin/router/` and `bin/end_device/`
3. For each file:
   - Determines the role (`router`/`end_device`) from the path
   - Determines the variant (`standard`/`forced`/`from_tuya`) from the filename suffix
   - Computes `sha256sum` to build the OCI blob URL
   - Selects the correct index file and board key
   - Calls `make_z2m_ota_index.py` to update the OTA index locally
   - Appends a line to the manifest file: `ZIGBEE_FILE|OCI_REF`
4. Validates all generated OTA index files (valid JSON, every entry has a valid `url`)

Arguments:
```
--tag TAG          Git tag (e.g. v1.2.3)
--owner OWNER      GitHub repository owner
--repo REPO        GitHub repository name
--db-file PATH     Path to device_db.yaml
--manifest PATH    Output path for the OCI push manifest file
```

The manifest file is a simple pipe-delimited text file, one line per `.zigbee` file:
```
bin/router/BOARD__MHCOZY_TS0004/tlc_switch-1.2.3-abc1234.zigbee|ghcr.io/owner/repo/BOARD__MHCOZY_TS0004:v1.2.3-router-standard
```

This script is testable locally without any OCI credentials or network access.

### 5. `.github/workflows/build.yml`

#### Permissions

Add `packages: write` so the workflow can push to `ghcr.io`:

```yaml
permissions:
  contents: write
  packages: write
```

#### Trigger and action versions

Change the trigger from `workflow_dispatch` only to also fire on tag push:
```yaml
on:
  workflow_dispatch:
  push:
    tags:
      - 'v*'
```

Update action versions: `actions/checkout@v2` → `@v4`, `actions/cache@v3` → `@v4`.

#### Build step

On both tagged and untagged builds: build all firmware. The `build` target no longer calls `update-indexes` (removed in step 3), so OTA indexes are not touched during the build phase. No changes to the existing `make board/build` calls are needed.

#### Upload build outputs as Actions artifact

The `build` job has **no side effects** — it does not commit, push, or upload to OCI. It only compiles and packages everything into an artifact:

```yaml
- name: Upload build outputs
  uses: actions/upload-artifact@v4
  with:
    name: build-outputs
    path: |
      bin/router/**/*.zigbee
      bin/end_device/**/*.zigbee
      zigbee2mqtt/
      zha/
      homed/
      docs/supported_devices.md
```

This artifact is downloadable from the workflow run page (retained 90 days), useful for testers. The `release` job downloads it to perform all side effects.

### `release` job (tag only)

A separate job that runs only on tagged builds. It downloads the build artifact, **prepares everything first** (OTA index, validation), then applies **side effects at the end**. Each side effect is idempotent so the job can be re-triggered safely.

```yaml
  release:
    needs: build
    if: startsWith(github.ref, 'refs/tags/v')
    runs-on: ubuntu-latest
    permissions:
      contents: write
      packages: write
    steps:
      # ── Preparation (no side effects) ──────────────────────────

      - uses: actions/checkout@v4

      - name: Download build outputs
        uses: actions/download-artifact@v4
        with:
          name: build-outputs

      - name: Set up Python and install dependencies
        # ... (same venv setup as build job)

      - name: Install ORAS
        uses: oras-project/setup-oras@v1

      - name: Log in to GitHub Container Registry
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Generate OTA index and OCI push manifest
        env:
          TAG: ${{ github.ref_name }}
          OWNER: ${{ github.repository_owner }}
          REPO: ${{ github.event.repository.name }}
        run: |
          source .venv/bin/activate
          python3 helper_scripts/prepare_release.py \
            --tag "$TAG" --owner "$OWNER" --repo "$REPO" \
            --db-file device_db.yaml \
            --manifest oci_push_manifest.txt

      # ── Side effects (idempotent, applied only after all prep succeeds) ──

      - name: Push all firmware to ghcr.io
        run: |
          # Each push is idempotent: re-pushing the same content+tag is a no-op
          while IFS='|' read -r ZIGBEE_FILE OCI_REF; do
            echo "Pushing $ZIGBEE_FILE → $OCI_REF"
            oras push "$OCI_REF" "$ZIGBEE_FILE"
          done < oci_push_manifest.txt

      - name: Commit all generated files
        run: |
          # Pull latest in case a previous partial run already committed
          git pull --rebase origin main

          # Stage everything: converters, quirks, docs, AND OTA index
          git add zha/* homed/* docs/supported_devices.md
          git add zigbee2mqtt/converters/* zigbee2mqtt/converters_v1/*
          git add zigbee2mqtt/ota/*

          # Idempotent: skip if nothing changed (e.g. re-run of same tag)
          git diff --cached --quiet || git commit -m "Release ${{ github.ref_name }}: update converters, quirks, and OTA index"
          git push origin HEAD:main

      - name: Create GitHub Release
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          # Idempotent: create if new, edit if already exists
          gh release create "${{ github.ref_name }}" \
            --generate-notes \
            --title "${{ github.ref_name }}" \
          || gh release edit "${{ github.ref_name }}" --generate-notes
```

**Design principles:**

1. **Preparation before side effects**: OTA index is fully generated and validated locally before any OCI push or git commit happens. The blob URLs are computed from `sha256sum` of local files — no network call needed.

2. **Atomicity**: If any preparation step fails (bad JSON, missing file, script error), no side effects run. Side effects are grouped at the end.

3. **Idempotency** (safe to re-trigger):
   - `oras push` with the same content+tag is a no-op (OCI content-addressing)
   - `git diff --cached --quiet` skips commit if already applied
   - `git pull --rebase` picks up any commits from a prior partial run
   - `gh release create || gh release edit` handles both first run and re-run

The `if: startsWith(github.ref, 'refs/tags/v')` appears once on the job, not on every step.

#### OCI blob URL — why `sha256sum` is correct

OCI registries store blobs content-addressed by their SHA-256 digest. The blob URL `https://ghcr.io/v2/{owner}/{repo}/{package}/blobs/sha256:{digest}` resolves directly to the raw file. The digest is simply `sha256sum` of the file content — no need to parse `oras push` output. This URL is valid and stable as long as the package is not deleted.

---

### 6. History cleanup (separate PR, coordinate with maintainers)

After the above is merged and working:

```bash
git filter-repo --path bin/ --invert-paths
git push --force origin main
```

This requires all contributors to re-clone. Coordinate with maintainers before doing it.

---

## OCI Package Structure

Each board gets one OCI package. For example:

```
ghcr.io/romasku/tuya-zigbee-switch/BOARD__MHCOZY_TS0004:v1.2.3-router-standard
ghcr.io/romasku/tuya-zigbee-switch/BOARD__MHCOZY_TS0004:v1.2.3-router-forced
ghcr.io/romasku/tuya-zigbee-switch/BOARD__MHCOZY_TS0004:v1.2.3-router-from_tuya
ghcr.io/romasku/tuya-zigbee-switch/BOARD__MHCOZY_TS0004:v1.2.3-end_device-standard
```

Packages are public, free, and permanently retained on `ghcr.io` for public repositories. No authentication is needed to download blobs.

---

## Caveats and Things to Verify Before Assuming the Plan is Complete

### 1. Read each file before editing — this plan describes the committed baseline

This plan was written against the post-`git reset --hard` state (commit `78da79c3`). The "Current committed state" notes in each file section above are accurate. Still: always read the file first before editing, particularly:

- **`helper_scripts/make_z2m_ota_index.py`**: Confirm `get_raw_github_link()` and `import subprocess` are still present before removing them.
- **`board.mk`**: Remove `update-indexes` from the `build` target deps. The `.PHONY` line is the last line of the file.
- **`build.yml`**: Confirm the trigger is still `workflow_dispatch` only before adding the tag trigger.

### 2. Verify that `sha256sum` equals the OCI blob digest

The plan constructs the blob URL using `sha256sum` of the local file:

```
https://ghcr.io/v2/{owner}/{repo}/{package}/blobs/sha256:{sha256sum_of_file}
```

This is correct **only if ORAS stores the file as-is without wrapping or compression**. ORAS pushes individual files as uncompressed blobs by default, so this should hold — but **verify it on the first real push** by running:

```bash
oras push ghcr.io/.../TEST:test ./somefile.zigbee --format json
```

Compare the `digest` field in the JSON output with `sha256sum somefile.zigbee`. They must match. If they don't, switch to parsing the `oras push --format json` output instead of using `sha256sum`.

### 3. `ghcr.io` package visibility

When `GITHUB_TOKEN` pushes a new package to `ghcr.io` for the first time, it inherits the repository's visibility. For a public repository this means the package is public automatically. However, **verify this on the first push** — navigate to `https://github.com/orgs/{owner}/packages` or the repo's Packages tab and confirm the new package shows as public. If it is private, go to package settings and set it to public manually, or add a workflow step using the GitHub API to set package visibility.

### 4. End-to-end testing before merging

After implementing the changes, test the full flow before opening a PR:

1. Push a test tag (e.g. `v0.0.0-test`) to a fork or the repo.
2. Wait for the workflow to complete.
3. Pick one entry from the generated OTA index and `curl -LI` the `url` field — confirm it returns a 200 with `Content-Type: application/octet-stream` and `Content-Length` matching `fileSize`.
4. Confirm the OTA index was committed to `main` and the GH Release was created without firmware attachments.
5. Delete the test tag and release afterwards.

---

## Open Question (pending maintainer reply)

Whether `from_tuya` variants will eventually be unified into a single generic binary. If yes, total release artifacts drop from ~650 to ~14 (7 MCU×role combinations × 2 index types), and GH Releases would be sufficient. If no, the ~650 per-board files remain and the 1,000-asset GH Release limit is a real constraint. This plan works correctly either way.

