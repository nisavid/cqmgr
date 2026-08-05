"""Trusted verification for untrusted pull-request candidate artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_pull_request_candidate.py"

REPOSITORY = "nisavid/cqmgr"
HEAD_SHA = "a" * 40
PULL_REQUEST = "105"


def _verify_candidate() -> Callable[..., Path]:
    return cast(
        "Callable[..., Path]",
        runpy.run_path(str(SCRIPT))["verify_candidate"],
    )


def _main() -> Callable[[list[str]], None]:
    return cast(
        "Callable[[list[str]], None]",
        runpy.run_path(str(SCRIPT))["main"],
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _candidate(
    root: Path,
    *,
    version: str = "0.1.0",
    asset_version: str | None = None,
) -> tuple[Path, Path]:
    candidate = root / "candidate"
    release = candidate / "release"
    release.mkdir(parents=True)
    asset_version = asset_version or version
    identity = {
        "commit": HEAD_SHA,
        "mode": "pull-request",
        "pull_request": PULL_REQUEST,
        "repository": REPOSITORY,
        "version": version,
    }
    _write_json(candidate / "pull-request-identity.json", identity)

    wheel = release / f"cqmgr-{asset_version}-py3-none-any.whl"
    sdist = release / f"cqmgr-{asset_version}.tar.gz"
    sbom = release / f"cqmgr-{asset_version}.cdx.json"
    wheel.write_bytes(b"wheel bytes")
    sdist.write_bytes(b"sdist bytes")
    sbom.write_bytes(b"sbom bytes")
    manifest = {
        "commit": HEAD_SHA,
        "distributions": [
            {
                "name": wheel.name,
                "sha256": _sha256(wheel),
                "size": wheel.stat().st_size,
            },
            {
                "name": sdist.name,
                "sha256": _sha256(sdist),
                "size": sdist.stat().st_size,
            },
        ],
        "publication": {"authorized": False, "requested": False},
        "pull_request": PULL_REQUEST,
        "repository": REPOSITORY,
        "sbom": {
            "format": "CycloneDX",
            "name": sbom.name,
            "sha256": _sha256(sbom),
            "spec_version": "1.6",
        },
        "schema": "cqmgr.release-manifest/v1",
        "version": identity["version"],
    }
    manifest_path = release / "release-manifest.json"
    _write_json(manifest_path, manifest)
    checksummed = (wheel, sdist, sbom, manifest_path)
    (release / "SHA256SUMS").write_text(
        "".join(f"{_sha256(path)}  {path.name}\n" for path in checksummed),
        encoding="utf-8",
        newline="\n",
    )
    return candidate, wheel


def test_verify_candidate_accepts_exact_identity_bound_asset_names(
    tmp_path: Path,
) -> None:
    """Exact verified manifest names select the identity-bound wheel."""
    candidate, wheel = _candidate(tmp_path)

    selected = _verify_candidate()(
        candidate,
        repository=REPOSITORY,
        pull_request=PULL_REQUEST,
        head_sha=HEAD_SHA,
    )

    assert selected == wheel


def test_verify_candidate_rejects_assets_named_for_another_version(
    tmp_path: Path,
) -> None:
    """Self-consistent bytes cannot substitute assets for another version."""
    candidate, _wheel = _candidate(
        tmp_path,
        version="0.1.0",
        asset_version="9.9.9",
    )

    with pytest.raises(ValueError, match="identity version"):
        _verify_candidate()(
            candidate,
            repository=REPOSITORY,
            pull_request=PULL_REQUEST,
            head_sha=HEAD_SHA,
        )


def test_verify_candidate_rejects_identity_or_byte_drift(tmp_path: Path) -> None:
    """Caller identity and the recorded byte set must both remain exact."""
    candidate, wheel = _candidate(tmp_path)
    wheel.write_bytes(b"different wheel bytes")

    with pytest.raises(ValueError, match="digest"):
        _verify_candidate()(
            candidate,
            repository=REPOSITORY,
            pull_request=PULL_REQUEST,
            head_sha=HEAD_SHA,
        )

    candidate, _wheel = _candidate(tmp_path / "other")
    with pytest.raises(ValueError, match="identity"):
        _verify_candidate()(
            candidate,
            repository=REPOSITORY,
            pull_request="106",
            head_sha=HEAD_SHA,
        )


def test_main_writes_only_the_exact_verified_wheel_output(tmp_path: Path) -> None:
    """The workflow receives one canonical output only after verification."""
    candidate, wheel = _candidate(tmp_path)
    github_output = tmp_path / "github-output"

    _main()(
        [
            "--candidate",
            str(candidate),
            "--repository",
            REPOSITORY,
            "--pull-request",
            PULL_REQUEST,
            "--head-sha",
            HEAD_SHA,
            "--github-output",
            str(github_output),
        ]
    )

    assert github_output.read_text(encoding="utf-8") == f"wheel={wheel}\n"


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics differ on Windows")
def test_verify_candidate_rejects_manifest_symlink_before_reading(
    tmp_path: Path,
) -> None:
    """Untrusted manifest indirection cannot be read before path validation."""
    candidate, _wheel = _candidate(tmp_path)
    manifest_path = candidate / "release" / "release-manifest.json"
    target = tmp_path / "manifest-target.json"
    target.write_text("not json\n", encoding="utf-8")
    manifest_path.unlink()
    manifest_path.symlink_to(target)

    with pytest.raises(ValueError, match="direct regular file"):
        _verify_candidate()(
            candidate,
            repository=REPOSITORY,
            pull_request=PULL_REQUEST,
            head_sha=HEAD_SHA,
        )


@pytest.mark.parametrize(
    "unsafe_name",
    ["../candidate.whl", "cqmgr-$(id)-py3-none-any.whl"],
)
def test_verify_candidate_rejects_unsafe_manifest_asset_names(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    """Untrusted asset names cannot escape the directory or inject shell input."""
    candidate, _wheel = _candidate(tmp_path)
    manifest_path = candidate / "release" / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["distributions"][0]["name"] = unsafe_name
    _write_json(manifest_path, manifest)

    with pytest.raises(ValueError, match="asset name"):
        _verify_candidate()(
            candidate,
            repository=REPOSITORY,
            pull_request=PULL_REQUEST,
            head_sha=HEAD_SHA,
        )
