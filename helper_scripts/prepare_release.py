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
5. Write the manifest file (one line per blob: ``local_path|oci_ref``).
6. Generate ORAS annotation JSON (annotations.json) from the manifest
    so ORAS pushes include meaningful package metadata.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
import logging
from dataclasses import dataclass


HELPERS_DIR = Path(__file__).resolve().parent


@dataclass
class ManifestLine:
    """A parsed manifest entry.

    Attributes:
        local: Local path to the firmware file (as Path).
        remote: OCI reference string (ghcr.io/owner/repo/package:tag-role-variant).
    """
    local: Path
    remote: str


# Inlineable: uses Python 3.13+ `hashlib.file_digest(path, 'sha256').hexdigest()`


def classify_zigbee_file(path: Path) -> tuple[str, str, str]:
    """Return ``(role, variant, board_dir)`` for a ``.zigbee`` file.

    This function is resilient to slightly different path layouts. It finds
    the role by searching the path parts for either ``router`` or
    ``end_device`` and chooses the following path segment as the board
    directory. The variant is inferred from filename suffixes.

    Raises SystemExit with a helpful message on unexpected layouts.
    """
    p = Path(path)
    parts = list(p.parts)

    # Find role in path parts
    role = None
    for part in parts:
        if part in ("router", "end_device"):
            role = part
            break
    if role is None:
        raise SystemExit(f"Cannot determine role (router|end_device) from path: {p}")

    # Board dir should follow the role segment; fall back to parent folder
    try:
        role_index = parts.index(role)
        board_dir = parts[role_index + 1]
    except (ValueError, IndexError):
        # Try the parent directory of the file
        board_dir = p.parent.name
        if not board_dir:
            raise SystemExit(f"Cannot determine board directory for file: {p}")

    # Variant detection from filename (case-sensitive suffix rules maintained)
    name = p.stem
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


def _sanitize_package(board_dir: str) -> str:
    """Convert a board directory name to a valid OCI package name.

    OCI repository names must be lowercase and only allow ``_``, ``__``,
    ``-``, or ``.`` as separators between alphanumeric segments.  Board
    names like ``PLUG___AUBESS_PM_TS011F`` contain triple underscores
    which are invalid.  We lowercase and replace every run of
    underscores with a single hyphen.
    """
    return re.sub(r"_+", "-", board_dir.lower())


def oci_ref(owner: str, repo: str, board_dir: str, tag: str, role: str, variant: str) -> str:
    """Build the full OCI reference for ``oras push``.

    One ghcr.io package per board, name sanitised for OCI compliance.
    """
    package = _sanitize_package(board_dir)
    return f"ghcr.io/{owner}/{repo}/{package}:{tag}-{role}-{variant}"


def blob_url(owner: str, repo: str, board_dir: str, digest: str) -> str:
    """Build the direct ghcr.io blob download URL."""
    package = _sanitize_package(board_dir)
    return f"https://ghcr.io/v2/{owner}/{repo}/{package}/blobs/sha256:{digest}"


def index_file_for(role: str, variant: str) -> Path:
    """Select the correct OTA index file."""
    if variant == "forced":
        return Path(f"zigbee2mqtt/ota/index_{role}-FORCE.json")
    return Path(f"zigbee2mqtt/ota/index_{role}.json")


def clean_indexes(index_files: list[Path]) -> None:
    """Reset the provided OTA index files to empty JSON arrays.

    :param index_files: List of Path objects pointing to index files.
    """
    for idx in index_files:
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


def validate_indexes(index_files: list[Path]) -> None:
    """Assert every index is valid JSON and every entry has a valid url.

    :param index_files: List of Path objects pointing to index files.
    """
    for idx in index_files:
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


def generate_annotations_from_manifest_lines(
    manifest_lines: list[ManifestLine],
    owner: str,
    repo: str,
    tag: str,
    output_path: Path,
) -> None:
    """Generate ORAS annotation JSON from an in-memory list of ManifestLine entries.

    Accepts a list of ``ManifestLine`` objects (each with ``local`` and
    ``remote`` attributes) and writes the annotations JSON to ``output_path``.
    This avoids re-reading a manifest file when the manifest is already
    available in memory.
    """
    annotations: dict[str, dict[str, str]] = {
        "$manifest": {
            "org.opencontainers.image.description": "Firmware blob for Zigbee device",
            "org.opencontainers.image.title": "Zigbee Firmware",
            "org.opencontainers.image.source": f"https://github.com/{owner}/{repo}",
            "org.opencontainers.image.documentation": f"https://github.com/{owner}/{repo}/blob/{tag}/readme.md",
            "org.opencontainers.image.version": tag,
            "org.opencontainers.image.ref.name": tag,
            "com.github.package.type": "firmware",
        }
    }

    for lineno, ml in enumerate(manifest_lines, 1):
        # Fail fast on malformed entries so caller sees the exact bad value.
        if not ml.remote:
            raise SystemExit(f"Bad manifest entry #{lineno}: {ml!r}")
        bn = ml.local.name
        annotations[bn] = {"org.opencontainers.image.title": bn}

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(annotations, indent=2) + "\n")
    except Exception as exc:
        raise SystemExit(f"Failed to write annotations file {output_path}: {exc}") from exc


def discover_zigbee_files() -> list[Path]:
    """Discover all .zigbee files under bin/router and bin/end_device.

    :return: Sorted list of Path objects pointing to .zigbee files.
    :rtype: list[Path]
    :raises SystemExit: If no files are found.
    """
    zigbee_files = sorted(
        list(Path("bin/router").rglob("*.zigbee"))
        + list(Path("bin/end_device").rglob("*.zigbee"))
    )
    if not zigbee_files:
        raise SystemExit("No .zigbee files found under bin/router/ or bin/end_device/")
    return zigbee_files


def process_zigbee_files(
    zigbee_files: list[Path],
    owner: str,
    repo: str,
    tag: str,
    db_file: Path,
 ) -> list[ManifestLine]:
    """Process each discovered .zigbee file.

    Updates OTA indexes and returns manifest lines (``local_path|oci_ref``).

    :param zigbee_files: List of discovered .zigbee file paths.
    :param owner: GitHub repository owner.
    :param repo: GitHub repository name.
    :param tag: Release tag.
    :param db_file: Path to device_db.yaml for index updates.
    :return: List of manifest lines to write to the manifest file.
    """
    manifest_lines: list[ManifestLine] = []
    for zf in zigbee_files:
        role, variant, board_dir = classify_zigbee_file(zf)
        board = board_key_from_dir(board_dir, role)
        digest = hashlib.file_digest(zf, "sha256").hexdigest()
        url = blob_url(owner, repo, board_dir, digest)
        ref = oci_ref(owner, repo, board_dir, tag, role, variant)
        idx = index_file_for(role, variant)

        logging.info("  %s → %s", zf, ref)
        update_index(zf, url, idx, board, db_file)

        manifest_lines.append(ManifestLine(local=zf, remote=ref))

    return manifest_lines


def write_manifest(manifest_path: Path, manifest_lines: list[ManifestLine]) -> None:
    """Write the manifest file and report the number of entries.

    :param manifest_path: Path to write the manifest to.
    :param manifest_lines: List of manifest lines to write (no trailing newline).
    """
    manifest_path.write_text("\n".join(f"{m.local}|{m.remote}" for m in manifest_lines) + "\n")
    logging.info("Manifest written to %s (%d entries)", manifest_path, len(manifest_lines))


def parse_args() -> argparse.Namespace:
    """Build and parse command-line arguments for prepare_release.

    Separated into its own function to make the CLI easier to test.
    """
    parser = argparse.ArgumentParser(description="Prepare release artifacts (no side effects)")
    parser.add_argument("--tag", required=True, help="Git tag, e.g. v1.2.3")
    parser.add_argument("--owner", required=True, help="GitHub repository owner")
    parser.add_argument("--repo", required=True, help="GitHub repository name")
    parser.add_argument("--db-file", required=True, type=Path, help="Path to device_db.yaml")
    parser.add_argument("--manifest", required=True, type=Path, help="Output manifest file")
    parser.add_argument("--annotations", required=False, type=Path, default=Path("annotations.json"), help="Output annotations JSON file")
    parser.add_argument(
        "--index-files",
        nargs="+",
        type=Path,
        required=True,
        help="Paths to OTA index files (space-separated).",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    
    args = parse_args()

    # 1. Clean indexes
    clean_indexes(args.index_files)

    # 2. Discover all .zigbee files
    zigbee_files = discover_zigbee_files()
    logging.info("Found %d .zigbee files", len(zigbee_files))

    # 3. Process each file
    manifest_lines = process_zigbee_files(
        zigbee_files, args.owner, args.repo, args.tag, args.db_file
    )

    # 4. Validate
    validate_indexes(args.index_files)

    # 5. Write manifest
    write_manifest(args.manifest, manifest_lines)

    # 6. Generate ORAS annotation JSON
    # Always generate ORAS annotation JSON for the manifest (used by oras_push_all.sh)
    # Use the in-memory manifest lines instead of re-reading the file.
    generate_annotations_from_manifest_lines(
        manifest_lines=manifest_lines,
        owner=args.owner,
        repo=args.repo,
        tag=args.tag,
        output_path=args.annotations,
    )
    logging.info("Annotations written to %s", args.annotations)

if __name__ == "__main__":
    main()
