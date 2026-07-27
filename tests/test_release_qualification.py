"""Immutable release identity and bundle contracts."""

from __future__ import annotations

import hashlib
import json
import runpy
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "release_qualification.py"
COMMIT = "a" * 40


def _module() -> dict[str, Any]:
    return runpy.run_path(str(SCRIPT))


def test_release_identity_binds_static_version_tag_repository_and_main() -> None:
    """A production release identity agrees before any publish authority exists."""
    release_identity = cast("Any", _module()["release_identity"])

    identity = release_identity(
        ROOT / "pyproject.toml",
        repository="nisavid/cqmgr",
        ref_type="tag",
        ref_name="v0.1.0",
        commit_sha=COMMIT,
        protected_main_sha=COMMIT,
        dry_run=False,
    )

    assert identity == {
        "commit": COMMIT,
        "mode": "release",
        "repository": "nisavid/cqmgr",
        "tag": "v0.1.0",
        "version": "0.1.0",
    }


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ({"repository": "other/project"}, "canonical repository"),
        ({"ref_type": "branch"}, "annotated version tag"),
        ({"ref_name": "v0.1.1"}, "project version"),
        ({"protected_main_sha": "b" * 40}, "protected main"),
        ({"commit_sha": "short", "protected_main_sha": "short"}, "commit SHA"),
    ],
)
def test_release_identity_fails_closed_on_disagreement(
    values: dict[str, str],
    match: str,
) -> None:
    """Every independently supplied release identity must agree."""
    release_identity = cast("Any", _module()["release_identity"])
    arguments = {
        "repository": "nisavid/cqmgr",
        "ref_type": "tag",
        "ref_name": "v0.1.0",
        "commit_sha": COMMIT,
        "protected_main_sha": COMMIT,
    }
    arguments.update(values)

    with pytest.raises(ValueError, match=match):
        release_identity(
            ROOT / "pyproject.toml",
            **arguments,
            dry_run=False,
        )


def test_dry_run_uses_release_identity_without_claiming_publication() -> None:
    """Manual qualification binds main bytes but cannot masquerade as a tag run."""
    release_identity = cast("Any", _module()["release_identity"])

    identity = release_identity(
        ROOT / "pyproject.toml",
        repository="nisavid/cqmgr",
        ref_type="branch",
        ref_name="main",
        commit_sha=COMMIT,
        protected_main_sha=COMMIT,
        dry_run=True,
    )

    assert identity["mode"] == "dry-run"
    assert identity["tag"] == "v0.1.0"


def test_release_bundle_preserves_exact_bytes_and_machine_readable_evidence(
    tmp_path: Path,
) -> None:
    """Preparation copies immutable distributions and describes every release asset."""
    module = _module()
    prepare_release_bundle = cast("Any", module["prepare_release_bundle"])
    verify_release_bundle = cast("Any", module["verify_release_bundle"])
    dist = tmp_path / "dist"
    output = tmp_path / "release"
    dist.mkdir()
    wheel = dist / "cqmgr-0.1.0-py3-none-any.whl"
    sdist = dist / "cqmgr-0.1.0.tar.gz"
    wheel.write_bytes(b"wheel bytes")
    sdist.write_bytes(b"sdist bytes")
    requirements = "\n".join(
        (
            "click==8.3.1 \\",
            "    --hash=sha256:" + "1" * 64,
            'google-auth==2.47.0 ; python_full_version >= "3.12"',
        )
    )
    identity = {
        "commit": COMMIT,
        "mode": "dry-run",
        "repository": "nisavid/cqmgr",
        "tag": "v0.1.0",
        "version": "0.1.0",
    }

    prepare_release_bundle(dist, output, identity, requirements)
    manifest = verify_release_bundle(output, identity)

    assert wheel.read_bytes() == (output / wheel.name).read_bytes()
    assert sdist.read_bytes() == (output / sdist.name).read_bytes()
    assert manifest["publication"]["authorized"] is False
    assert {item["name"] for item in manifest["distributions"]} == {
        wheel.name,
        sdist.name,
    }
    sbom = json.loads((output / "cqmgr-0.1.0.cdx.json").read_text())
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.6"
    assert {(item["name"], item["version"]) for item in sbom["components"]} == {
        ("click", "8.3.1"),
        ("google-auth", "2.47.0"),
    }
    checksums = (output / "SHA256SUMS").read_text().splitlines()
    expected_checksum_count = 4
    assert len(checksums) == expected_checksum_count
    for line in checksums:
        digest, name = line.split("  ", 1)
        assert digest == hashlib.sha256((output / name).read_bytes()).hexdigest()


def test_release_bundle_detects_changed_distribution_bytes(tmp_path: Path) -> None:
    """No downstream job can replace a tested artifact without invalidating evidence."""
    module = _module()
    prepare_release_bundle = cast("Any", module["prepare_release_bundle"])
    verify_release_bundle = cast("Any", module["verify_release_bundle"])
    dist = tmp_path / "dist"
    output = tmp_path / "release"
    dist.mkdir()
    (dist / "cqmgr-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "cqmgr-0.1.0.tar.gz").write_bytes(b"sdist")
    identity = {
        "commit": COMMIT,
        "mode": "dry-run",
        "repository": "nisavid/cqmgr",
        "tag": "v0.1.0",
        "version": "0.1.0",
    }
    prepare_release_bundle(dist, output, identity, "click==8.3.1")
    (output / "cqmgr-0.1.0-py3-none-any.whl").write_bytes(b"replacement")

    with pytest.raises(ValueError, match="checksum"):
        verify_release_bundle(output, identity)
