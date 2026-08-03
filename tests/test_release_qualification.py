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


def test_pull_request_candidate_identity_binds_repo_number_and_exact_head() -> None:
    """Qualification identifies PR bytes without claiming a tag or protected main."""
    candidate_identity = cast("Any", _module()["pull_request_candidate_identity"])

    identity = candidate_identity(
        ROOT / "pyproject.toml",
        repository="nisavid/cqmgr",
        pull_request_number=123,
        head_sha=COMMIT,
    )

    assert identity == {
        "commit": COMMIT,
        "mode": "pull-request",
        "pull_request": "123",
        "repository": "nisavid/cqmgr",
        "version": "0.1.0",
    }
    assert "tag" not in identity


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ({"repository": "other/project"}, "canonical repository"),
        ({"pull_request_number": 0}, "positive integer"),
        ({"pull_request_number": True}, "positive integer"),
        ({"head_sha": "short"}, "head commit SHA"),
        ({"head_sha": "A" * 40}, "head commit SHA"),
    ],
)
def test_pull_request_candidate_identity_fails_closed_on_invalid_event_facts(
    values: dict[str, object],
    match: str,
) -> None:
    """Malformed or noncanonical event identity never enters qualification."""
    candidate_identity = cast("Any", _module()["pull_request_candidate_identity"])
    arguments: dict[str, object] = {
        "repository": "nisavid/cqmgr",
        "pull_request_number": 123,
        "head_sha": COMMIT,
    }
    arguments.update(values)

    with pytest.raises(ValueError, match=match):
        candidate_identity(ROOT / "pyproject.toml", **arguments)


def test_pull_request_bundle_is_verifiable_only_for_qualification(
    tmp_path: Path,
) -> None:
    """Candidate bytes qualify, while release and publication paths reject them."""
    module = _module()
    candidate_identity = cast("Any", module["pull_request_candidate_identity"])
    prepare_release_bundle = cast("Any", module["prepare_release_bundle"])
    verify_release_bundle = cast("Any", module["verify_release_bundle"])
    dist = tmp_path / "dist"
    output = tmp_path / "candidate"
    dist.mkdir()
    (dist / "cqmgr-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "cqmgr-0.1.0.tar.gz").write_bytes(b"sdist")
    identity = candidate_identity(
        ROOT / "pyproject.toml",
        repository="nisavid/cqmgr",
        pull_request_number=123,
        head_sha=COMMIT,
    )

    with pytest.raises(ValueError, match="non-publishable"):
        prepare_release_bundle(dist, output, identity, "click==8.3.1")

    prepare_release_bundle(
        dist,
        output,
        identity,
        "click==8.3.1",
        qualification=True,
    )

    with pytest.raises(ValueError, match="non-publishable"):
        verify_release_bundle(output, identity)
    manifest = verify_release_bundle(output, identity, qualification=True)
    assert manifest["pull_request"] == "123"
    assert "tag" not in manifest
    assert manifest["publication"] == {
        "authorized": False,
        "requested": False,
    }


def test_pull_request_bundle_cannot_claim_a_release_tag(tmp_path: Path) -> None:
    """Qualification rejects candidate evidence that also pretends to be a release."""
    module = _module()
    candidate_identity = cast("Any", module["pull_request_candidate_identity"])
    prepare_release_bundle = cast("Any", module["prepare_release_bundle"])
    verify_release_bundle = cast("Any", module["verify_release_bundle"])
    dist = tmp_path / "dist"
    output = tmp_path / "candidate"
    dist.mkdir()
    (dist / "cqmgr-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "cqmgr-0.1.0.tar.gz").write_bytes(b"sdist")
    identity = candidate_identity(
        ROOT / "pyproject.toml",
        repository="nisavid/cqmgr",
        pull_request_number=123,
        head_sha=COMMIT,
    )
    prepare_release_bundle(
        dist,
        output,
        identity,
        "click==8.3.1",
        qualification=True,
    )
    manifest_path = output / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tag"] = "v0.1.0"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    checksums_path = output / "SHA256SUMS"
    checksums_path.write_text(
        "\n".join(
            (
                f"{hashlib.sha256(manifest_path.read_bytes()).hexdigest()}"
                "  release-manifest.json"
                if line.endswith("  release-manifest.json")
                else line
            )
            for line in checksums_path.read_text(encoding="utf-8").splitlines()
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ValueError, match="identity fields"):
        verify_release_bundle(output, identity, qualification=True)


def test_pull_request_verification_recomputes_the_event_identity(
    tmp_path: Path,
) -> None:
    """A consumer distrusts downloaded identity and binds it to its own PR context."""
    module = _module()
    candidate_identity = cast("Any", module["pull_request_candidate_identity"])
    prepare_release_bundle = cast("Any", module["prepare_release_bundle"])
    verify_pull_request_candidate = cast(
        "Any",
        module["verify_pull_request_candidate"],
    )
    dist = tmp_path / "dist"
    output = tmp_path / "candidate"
    dist.mkdir()
    (dist / "cqmgr-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "cqmgr-0.1.0.tar.gz").write_bytes(b"sdist")
    identity = candidate_identity(
        ROOT / "pyproject.toml",
        repository="nisavid/cqmgr",
        pull_request_number=123,
        head_sha=COMMIT,
    )
    prepare_release_bundle(
        dist,
        output,
        identity,
        "click==8.3.1",
        qualification=True,
    )

    manifest = verify_pull_request_candidate(
        output,
        identity,
        ROOT / "pyproject.toml",
        repository="nisavid/cqmgr",
        pull_request_number=123,
        head_sha=COMMIT,
    )

    assert manifest["commit"] == COMMIT
    with pytest.raises(ValueError, match="event identity"):
        verify_pull_request_candidate(
            output,
            identity,
            ROOT / "pyproject.toml",
            repository="nisavid/cqmgr",
            pull_request_number=123,
            head_sha="b" * 40,
        )


def test_pull_request_cli_prepares_and_independently_verifies_candidate(
    tmp_path: Path,
) -> None:
    """The workflow-facing commands keep PR preparation in qualification mode."""
    main = cast("Any", _module()["main"])
    dist = tmp_path / "dist"
    output = tmp_path / "candidate"
    identity = tmp_path / "candidate-identity.json"
    requirements = tmp_path / "requirements.txt"
    dist.mkdir()
    (dist / "cqmgr-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "cqmgr-0.1.0.tar.gz").write_bytes(b"sdist")
    requirements.write_text("click==8.3.1\n", encoding="utf-8", newline="\n")

    main(
        [
            "pull-request-identity",
            "--project",
            str(ROOT / "pyproject.toml"),
            "--repository",
            "nisavid/cqmgr",
            "--pull-request",
            "123",
            "--head-commit",
            COMMIT,
            "--output",
            str(identity),
        ]
    )
    main(
        [
            "prepare",
            "--dist",
            str(dist),
            "--identity",
            str(identity),
            "--requirements",
            str(requirements),
            "--output",
            str(output),
            "--qualification",
        ]
    )
    main(
        [
            "verify-pull-request",
            "--release",
            str(output),
            "--identity",
            str(identity),
            "--project",
            str(ROOT / "pyproject.toml"),
            "--repository",
            "nisavid/cqmgr",
            "--pull-request",
            "123",
            "--head-commit",
            COMMIT,
        ]
    )

    assert json.loads(identity.read_text(encoding="utf-8"))["mode"] == ("pull-request")


@pytest.mark.parametrize(
    "project_text",
    [
        ('[project]\nname = "cqmgr"\nversion = "0.1.0"\ndynamic = ["version"]\n'),
        '[project]\nname = "cqmgr"\ndynamic = ["version"]\n',
        '[project]\nname = "cqmgr"\nversion = ""\n',
    ],
)
def test_project_version_must_be_one_static_nonempty_string(
    tmp_path: Path,
    project_text: str,
) -> None:
    """Dynamic or empty versions cannot enter the immutable release identity."""
    project_path = tmp_path / "pyproject.toml"
    project_path.write_text(project_text, encoding="utf-8", newline="\n")
    project_version = cast("Any", _module()["_project_version"])

    with pytest.raises(ValueError, match="static non-empty string"):
        project_version(project_path)


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
    sbom = json.loads((output / "cqmgr-0.1.0.cdx.json").read_text(encoding="utf-8"))
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.6"
    assert {(item["name"], item["version"]) for item in sbom["components"]} == {
        ("click", "8.3.1"),
        ("google-auth", "2.47.0"),
    }
    checksums = (output / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
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


def test_release_bundle_rejects_duplicate_checksum_entries(tmp_path: Path) -> None:
    """SHA256SUMS cannot hide a duplicate asset behind last-write-wins parsing."""
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
    checksums_path = output / "SHA256SUMS"
    first_line = checksums_path.read_text(encoding="utf-8").splitlines()[0]
    with checksums_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(first_line + "\n")

    with pytest.raises(ValueError, match="duplicate"):
        verify_release_bundle(output, identity)


def test_loaded_release_identity_requires_every_bound_field(tmp_path: Path) -> None:
    """A persisted identity missing its version cannot reach bundle verification."""
    load_identity = cast("Any", _module()["_load_identity"])
    identity_path = tmp_path / "release-identity.json"
    identity_path.write_text(
        json.dumps(
            {
                "commit": COMMIT,
                "mode": "release",
                "repository": "nisavid/cqmgr",
                "tag": "v0.1.0",
            }
        ),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ValueError, match="exact required fields"):
        load_identity(identity_path)


def test_sbom_normalizes_pep_503_equivalent_dependency_names() -> None:
    """Equivalent requirement spellings identify one SBOM component."""
    requirements_components = cast("Any", _module()["_requirements_components"])

    components = requirements_components(
        "oslo.concurrency==7.2.0\noslo_concurrency==7.2.0"
    )

    assert components == [
        {
            "bom-ref": "pkg:pypi/oslo-concurrency@7.2.0",
            "name": "oslo-concurrency",
            "purl": "pkg:pypi/oslo-concurrency@7.2.0",
            "type": "library",
            "version": "7.2.0",
        }
    ]


def test_sbom_rejects_conflicting_normalized_dependency_pins() -> None:
    """One normalized package identity cannot carry two release versions."""
    requirements_components = cast("Any", _module()["_requirements_components"])

    with pytest.raises(ValueError, match="conflicting pins"):
        requirements_components("google_auth==2.0\ngoogle-auth==3.0\n")


def test_sbom_rejects_unsupported_requirement_syntax() -> None:
    """Unsupported requirement forms cannot silently disappear from the SBOM."""
    requirements_components = cast("Any", _module()["_requirements_components"])

    with pytest.raises(ValueError, match="unsupported unpinned line"):
        requirements_components("google-auth[enterprise]==2.0\n")


def test_release_bundle_rejects_duplicate_distribution_entries(tmp_path: Path) -> None:
    """A manifest cannot hide duplicated distribution names behind a mapping."""
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
    manifest_path = output / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["distributions"] = [
        manifest["distributions"][0],
        manifest["distributions"][0],
    ]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    checksums_path = output / "SHA256SUMS"
    lines = [
        (
            f"{hashlib.sha256(manifest_path.read_bytes()).hexdigest()}"
            "  release-manifest.json"
            if line.endswith("  release-manifest.json")
            else line
        )
        for line in checksums_path.read_text(encoding="utf-8").splitlines()
    ]
    checksums_path.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ValueError, match="names must be unique"):
        verify_release_bundle(output, identity)
