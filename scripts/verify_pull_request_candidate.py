"""Verify one untrusted pull-request candidate from trusted workflow source."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

CANONICAL_REPOSITORY = "nisavid/cqmgr"
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
DIRECT_ASSET_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
PULL_REQUEST_PATTERN = re.compile(r"[1-9][0-9]*\Z")
IDENTITY_FIELDS = {
    "commit",
    "mode",
    "pull_request",
    "repository",
    "version",
}
MANIFEST_FIELDS = {
    "commit",
    "distributions",
    "publication",
    "pull_request",
    "repository",
    "sbom",
    "schema",
    "version",
}
MANIFEST_SCHEMA = "cqmgr.release-manifest/v1"
ASCII_CONTROL_LIMIT = 32
ASCII_DELETE = 127
EXPECTED_DISTRIBUTION_COUNT = 2


def _object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        msg = f"{path.name} must contain one string-keyed object"
        raise TypeError(msg)
    return cast("dict[str, object]", value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _asset_name(value: object, *, kind: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or DIRECT_ASSET_PATTERN.fullmatch(value) is None
        or value in {".", ".."}
        or Path(value).name != value
        or "/" in value
        or "\\" in value
        or any(
            ord(character) < ASCII_CONTROL_LIMIT or ord(character) == ASCII_DELETE
            for character in value
        )
    ):
        msg = f"candidate {kind} asset name is unsafe"
        raise ValueError(msg)
    return value


def _digest(value: object, *, kind: str) -> str:
    if not isinstance(value, str) or DIGEST_PATTERN.fullmatch(value) is None:
        msg = f"candidate {kind} digest is invalid"
        raise ValueError(msg)
    return value


def _identity(
    path: Path,
    *,
    repository: str,
    pull_request: str,
    head_sha: str,
) -> dict[str, str]:
    value = _object(path)
    if set(value) != IDENTITY_FIELDS or not all(
        isinstance(item, str) and item for item in value.values()
    ):
        msg = "candidate identity must contain the exact required string fields"
        raise ValueError(msg)
    identity = cast("dict[str, str]", value)
    expected = {
        "commit": head_sha,
        "mode": "pull-request",
        "pull_request": pull_request,
        "repository": repository,
        "version": identity["version"],
    }
    if identity != expected:
        msg = "candidate identity does not match the caller"
        raise ValueError(msg)
    if repository != CANONICAL_REPOSITORY or COMMIT_PATTERN.fullmatch(head_sha) is None:
        msg = "candidate identity does not name the canonical repository and head"
        raise ValueError(msg)
    if PULL_REQUEST_PATTERN.fullmatch(pull_request) is None:
        msg = "candidate identity has an invalid pull-request number"
        raise ValueError(msg)
    version = identity["version"]
    if version.strip() != version or any(
        ord(character) < ASCII_CONTROL_LIMIT or ord(character) == ASCII_DELETE
        for character in version
    ):
        msg = "candidate identity has an unsafe version"
        raise ValueError(msg)
    return identity


def _distribution_assets(
    value: object,
    *,
    version: str,
) -> tuple[dict[str, tuple[str, int]], str]:
    if not isinstance(value, list) or len(value) != EXPECTED_DISTRIBUTION_COUNT:
        msg = "candidate manifest must describe exactly two distributions"
        raise ValueError(msg)
    recorded: dict[str, tuple[str, int]] = {}
    wheel = _asset_name(
        f"cqmgr-{version}-py3-none-any.whl",
        kind="identity version",
    )
    sdist = _asset_name(
        f"cqmgr-{version}.tar.gz",
        kind="identity version",
    )
    for raw_item in value:
        if not isinstance(raw_item, dict) or set(raw_item) != {
            "name",
            "sha256",
            "size",
        }:
            msg = "candidate distribution record is malformed"
            raise ValueError(msg)
        item = cast("Mapping[str, object]", raw_item)
        name = _asset_name(item["name"], kind="distribution")
        digest = _digest(item["sha256"], kind="distribution")
        size = item["size"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            msg = "candidate distribution size is invalid"
            raise ValueError(msg)
        if name in recorded:
            msg = "candidate distribution names must be unique"
            raise ValueError(msg)
        recorded[name] = (digest, size)
    if set(recorded) != {wheel, sdist}:
        msg = "candidate distribution names do not match the identity version"
        raise ValueError(msg)
    return recorded, wheel


def _checksum_mapping(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.endswith("\n") or "\r" in text:
        msg = "candidate checksum file is not canonical"
        raise ValueError(msg)
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        try:
            raw_digest, raw_name = line.split("  ", 1)
        except ValueError as error:
            msg = "candidate checksum line is malformed"
            raise ValueError(msg) from error
        name = _asset_name(raw_name, kind="checksum")
        digest = _digest(raw_digest, kind="checksum")
        if name in checksums:
            msg = "candidate checksum names must be unique"
            raise ValueError(msg)
        checksums[name] = digest
    return checksums


def verify_candidate(  # noqa: C901, PLR0912, PLR0915 - one fail-closed audit
    candidate: Path,
    *,
    repository: str,
    pull_request: str,
    head_sha: str,
) -> Path:
    """Return the single wheel only after exact identity and byte verification."""
    release = candidate / "release"
    identity_path = candidate / "pull-request-identity.json"
    if candidate.is_symlink() or not candidate.is_dir():
        msg = "candidate root must be one direct directory"
        raise ValueError(msg)
    candidate_entries = tuple(candidate.iterdir())
    if (
        {path.name for path in candidate_entries}
        != {"pull-request-identity.json", "release"}
        or identity_path.is_symlink()
        or not identity_path.is_file()
        or release.is_symlink()
        or not release.is_dir()
    ):
        msg = "candidate root contains missing or unexpected entries"
        raise ValueError(msg)
    identity = _identity(
        identity_path,
        repository=repository,
        pull_request=pull_request,
        head_sha=head_sha,
    )

    manifest_path = release / "release-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        msg = "candidate manifest must be one direct regular file"
        raise ValueError(msg)
    manifest = _object(manifest_path)
    if (
        set(manifest) != MANIFEST_FIELDS
        or manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("commit") != identity["commit"]
        or manifest.get("pull_request") != identity["pull_request"]
        or manifest.get("repository") != identity["repository"]
        or manifest.get("version") != identity["version"]
        or manifest.get("publication") != {"authorized": False, "requested": False}
    ):
        msg = "candidate manifest identity is invalid"
        raise ValueError(msg)
    distributions, wheel_name = _distribution_assets(
        manifest["distributions"],
        version=identity["version"],
    )
    raw_sbom = manifest["sbom"]
    if not isinstance(raw_sbom, dict) or set(raw_sbom) != {
        "format",
        "name",
        "sha256",
        "spec_version",
    }:
        msg = "candidate SBOM record is malformed"
        raise ValueError(msg)
    sbom = cast("Mapping[str, object]", raw_sbom)
    sbom_name = _asset_name(sbom["name"], kind="SBOM")
    expected_sbom_name = _asset_name(
        f"cqmgr-{identity['version']}.cdx.json",
        kind="identity version",
    )
    sbom_digest = _digest(sbom["sha256"], kind="SBOM")
    if (
        sbom.get("format") != "CycloneDX"
        or sbom.get("spec_version") != "1.6"
        or sbom_name != expected_sbom_name
    ):
        msg = "candidate SBOM identity is invalid"
        raise ValueError(msg)

    expected_hashed = {*distributions, sbom_name, manifest_path.name}
    expected_assets = {*expected_hashed, "SHA256SUMS"}
    assets = tuple(release.iterdir())
    if (
        any(path.is_symlink() or not path.is_file() for path in assets)
        or {path.name for path in assets} != expected_assets
    ):
        msg = "candidate release contains missing or unexpected assets"
        raise ValueError(msg)
    checksums = _checksum_mapping(release / "SHA256SUMS")
    if set(checksums) != expected_hashed:
        msg = "candidate checksum coverage does not match the exact asset set"
        raise ValueError(msg)
    for name, expected_digest in checksums.items():
        if _sha256(release / name) != expected_digest:
            msg = f"candidate byte digest mismatch for {name}"
            raise ValueError(msg)
    for name, (digest, size) in distributions.items():
        path = release / name
        if checksums[name] != digest or path.stat().st_size != size:
            msg = f"candidate distribution metadata mismatch for {name}"
            raise ValueError(msg)
    if checksums[sbom_name] != sbom_digest:
        msg = "candidate SBOM metadata does not match exact bytes"
        raise ValueError(msg)
    return release / wheel_name


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pull-request", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--github-output", required=True, type=Path)
    return parser


def main(arguments: Sequence[str] | None = None) -> None:
    """Verify one candidate and expose its exact wheel to the trusted workflow."""
    parsed = _parser().parse_args(arguments)
    wheel = verify_candidate(
        parsed.candidate,
        repository=parsed.repository,
        pull_request=parsed.pull_request,
        head_sha=parsed.head_sha,
    )
    with parsed.github_output.open("a", encoding="utf-8", newline="\n") as output:
        output.write(f"wheel={wheel}\n")


if __name__ == "__main__":
    main()
