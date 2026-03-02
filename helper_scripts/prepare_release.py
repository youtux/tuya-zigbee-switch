#!/usr/bin/env python3
"""Prepare a release: generate OTA indexes and an OCI push manifest.

This script has **no side effects** — it does not make network calls,
push to OCI, or run git commands.  It is safe to run locally without
credentials.

Steps performed:
1. Clean all OTA index files (reset to empty JSON arrays).
2. Walk ``bin/router/`` and ``bin/end_device/`` for ``.zigbee`` files.
3. For each file, compute the OCI blob URL from its SHA-256 digest,
   call ``make_z2m_ota_index.py`` to update the correct index file,
   and append a line to the manifest.
4. Validate every generated index (valid JSON, every ``url`` starts
   with ``https://``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


# The four OTA index files, matching tools.mk clean_z2m_index
INDEX_FILES = [
    Path("zigbee2mqtt/ota/index_router.json"),
    Path("zigbee2mqtt/ota/index_router-FORCE.json"),
    Path("zigbee2mqtt/ota/index_end_device.json"),
    Path("zigbee2mqtt/ota/index_end_device-FORCE.json"),
]

HELPERS_DIR = Path(__file__).resolve().parent


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def classify_zigbee_file(path: Path) -> tuple[str, str, str]:
    """Return ``(role, variant, board_dir)`` for a ``.zigbee`` file.

    ``role`` is ``router`` or ``end_device``.
    ``variant`` is ``standard``, ``forced``, or ``from_tuya``.
    ``board_dir`` is the directory name (e.g. ``BOARD__MHCOZY_TS0004``).
    """
    parts = path.parts  # e.g. ('bin', 'router', 'BOARD__…', 'file.zigbee')
    role = parts[1]  # 'router' or 'end_device'
    board_dir = parts[2]

    name = path.stem  # e.g. 'tlc_switch-1.2.3-abc1234-forced'
    if name.endswith("-forced"):
        variant = "forced"
    elif name.endswith("-from_tuya"):
        variant = "from_tuya"
    else:
        variant = "standard"

    return role, variant, board_dir


def board_key_from_dir(board_dir: str, role: str) -> str:
    """Derive the device_db.yaml key from the directory name.

    For router builds the dir name *is* the key.
    For end_device builds the dir has an ``_END_DEVICE`` suffix that
    is **not** part of the db key.
    """
    if role == "end_device" and board_dir.endswith("_END_DEVICE"):
        return board_dir[: -len("_END_DEVICE")]
    return board_dir


def oci_ref(owner: str, repo: str, board_dir: str, tag: str, role: str, variant: str) -> str:
    """Build the full OCI reference for ``oras push``."""
    return f"ghcr.io/{owner}/{repo}/{board_dir}:{tag}-{role}-{variant}"


def blob_url(owner: str, repo: str, board_dir: str, digest: str) -> str:
    """Build the direct ghcr.io blob download URL."""
    return f"https://ghcr.io/v2/{owner}/{repo}/{board_dir}/blobs/sha256:{digest}"


def index_file_for(role: str, variant: str) -> Path:
    """Select the correct OTA index file."""
    if variant == "forced":
        return Path(f"zigbee2mqtt/ota/index_{role}-FORCE.json")
    return Path(f"zigbee2mqtt/ota/index_{role}.json")


def clean_indexes() -> None:
    for idx in INDEX_FILES:
        idx.parent.mkdir(parents=True, exist_ok=True)
        idx.write_text("[]\n")


def update_index(
    zigbee_file: Path,
    url: str,
    index_file: Path,
    board_key: str,
    db_file: Path,
) -> None:
    """Call make_z2m_ota_index.py to upsert one entry."""
    cmd = [
        sys.executable,
        str(HELPERS_DIR / "make_z2m_ota_index.py"),
        str(zigbee_file),
        str(index_file),
        "--url", url,
        "--db_file", str(db_file),
        "--board", board_key,
    ]
    subprocess.run(cmd, check=True)


def validate_indexes() -> None:
    """Assert every index is valid JSON and every entry has a valid url."""
    for idx in INDEX_FILES:
        if not idx.exists():
            continue
        try:
            entries = json.loads(idx.read_text())
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSON in {idx}: {exc}") from exc
        for entry in entries:
            entry_url = entry.get("url", "")
            if not entry_url.startswith("https://"):
                raise SystemExit(
                    f"Bad url in {idx} for imageType={entry.get('imageType')}: {entry_url!r}"
                )


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare release artifacts (no side effects)")
    parser.add_argument("--tag", required=True, help="Git tag, e.g. v1.2.3")
    parser.add_argument("--owner", required=True, help="GitHub repository owner")
    parser.add_argument("--repo", required=True, help="GitHub repository name")
    parser.add_argument("--db-file", required=True, type=Path, help="Path to device_db.yaml")
    parser.add_argument("--manifest", required=True, type=Path, help="Output manifest file")
    args = parser.parse_args()

    # 1. Clean indexes
    clean_indexes()

    # 2. Discover all .zigbee files
    zigbee_files = sorted(
        list(Path("bin/router").rglob("*.zigbee"))
        + list(Path("bin/end_device").rglob("*.zigbee"))
    )
    if not zigbee_files:
        raise SystemExit("No .zigbee files found under bin/router/ or bin/end_device/")

    print(f"Found {len(zigbee_files)} .zigbee files")

    # 3. Process each file
    manifest_lines: list[str] = []
    for zf in zigbee_files:
        role, variant, board_dir = classify_zigbee_file(zf)
        board = board_key_from_dir(board_dir, role)
        digest = sha256_of_file(zf)
        url = blob_url(args.owner, args.repo, board_dir, digest)
        ref = oci_ref(args.owner, args.repo, board_dir, args.tag, role, variant)
        idx = index_file_for(role, variant)

        print(f"  {zf} → {ref}")
        update_index(zf, url, idx, board, args.db_file)
        manifest_lines.append(f"{zf}|{ref}")

    # 4. Validate
    validate_indexes()

    # Write manifest
    args.manifest.write_text("\n".join(manifest_lines) + "\n")
    print(f"Manifest written to {args.manifest} ({len(manifest_lines)} entries)")


if __name__ == "__main__":
    main()
