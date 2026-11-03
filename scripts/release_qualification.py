"""Prepare and verify one immutable cqmgr release candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

CANONICAL_REPOSITORY = "nisavid/cqmgr"
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
PIN_PATTERN = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;\\]+)"
)
RELEASE_MANIFEST_SCHEMA = "cqmgr.release-manifest/v1"
SBOM_FILENAME_TEMPLATE = "cqmgr-{version}.cdx.json"
RELEASE_IDENTITY_FIELDS = {
    "commit",
    "mode",
    "repository",
    "tag",
    "version",
}
PULL_REQUEST_CANDIDATE_IDENTITY_FIELDS = {
    "commit",
    "mode",
    "pull_request",
    "repository",
    "version",
}


def _project_version(project_path: Path) -> str:
    document = tomllib.loads(project_path.read_text(encoding="utf-8"))
    project = document.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    dynamic = project.get("dynamic") if isinstance(project, dict) else None
    if (
        not isinstance(version, str)
        or not version.strip()
        or (isinstance(dynamic, list) and "version" in dynamic)
    ):
        msg = "project version must be one static non-empty string"
        raise ValueError(msg)
    return version


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def release_identity(  # noqa: PLR0913 - independent identity facts must agree
    project_path: Path,
    *,
    repository: str,
    ref_type: str,
    ref_name: str,
    commit_sha: str,
    protected_main_sha: str,
    dry_run: bool,
) -> dict[str, str]:
    """Bind repository, static version, ref, and protected-main commit."""
    if repository != CANONICAL_REPOSITORY:
        msg = f"release must target the canonical repository {CANONICAL_REPOSITORY}"
        raise ValueError(msg)
    if not COMMIT_PATTERN.fullmatch(commit_sha) or not COMMIT_PATTERN.fullmatch(
        protected_main_sha
    ):
        msg = "release identity requires lowercase full commit SHA values"
        raise ValueError(msg)
    if commit_sha != protected_main_sha:
        msg = "release commit must equal the current protected main commit"
        raise ValueError(msg)
    version = _project_version(project_path)
    tag = f"v{version}"
    if dry_run:
        if ref_type != "branch" or ref_name != "main":
            msg = "dry-run qualification must execute from the main branch"
            raise ValueError(msg)
        mode = "dry-run"
    else:
        if ref_type != "tag":
            msg = "release publication requires one annotated version tag"
            raise ValueError(msg)
        if ref_name != tag:
            msg = f"release tag must agree with project version {version}"
            raise ValueError(msg)
        mode = "release"
    return {
        "commit": commit_sha,
        "mode": mode,
        "repository": repository,
        "tag": tag,
        "version": version,
    }


def pull_request_candidate_identity(
    project_path: Path,
    *,
    repository: str,
    pull_request_number: int,
    head_sha: str,
) -> dict[str, str]:
    """Bind a non-publishable pull-request candidate to its exact head."""
    if repository != CANONICAL_REPOSITORY:
        msg = f"candidate must target the canonical repository {CANONICAL_REPOSITORY}"
        raise ValueError(msg)
    if (
        isinstance(pull_request_number, bool)
        or not isinstance(pull_request_number, int)
        or pull_request_number < 1
    ):
        msg = "candidate pull-request number must be a positive integer"
        raise ValueError(msg)
    if not COMMIT_PATTERN.fullmatch(head_sha):
        msg = "candidate identity requires one lowercase full head commit SHA"
        raise ValueError(msg)
    return {
        "commit": head_sha,
        "mode": "pull-request",
        "pull_request": str(pull_request_number),
        "repository": repository,
        "version": _project_version(project_path),
    }


def _requirements_components(requirements: str) -> list[dict[str, str]]:
    components: dict[str, str] = {}
    for raw_line in requirements.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-", "\\")):
            continue
        match = PIN_PATTERN.match(line)
        if match is None:
            msg = f"requirements contain an unsupported unpinned line: {line}"
            raise ValueError(msg)
        name = re.sub(r"[-_.]+", "-", match.group("name")).lower()
        version = match.group("version")
        previous = components.setdefault(name, version)
        if previous != version:
            msg = f"requirements contain conflicting pins for {name}"
            raise ValueError(msg)
    return [
        {
            "bom-ref": f"pkg:pypi/{name}@{version}",
            "name": name,
            "purl": f"pkg:pypi/{name}@{version}",
            "type": "library",
            "version": version,
        }
        for name, version in sorted(components.items())
    ]


def _sbom(identity: Mapping[str, str], requirements: str) -> dict[str, object]:
    version = identity["version"]
    return {
        "bomFormat": "CycloneDX",
        "components": _requirements_components(requirements),
        "metadata": {
            "component": {
                "bom-ref": f"pkg:pypi/cqmgr@{version}",
                "name": "cqmgr",
                "purl": f"pkg:pypi/cqmgr@{version}",
                "type": "application",
                "version": version,
            },
            "properties": [
                {
                    "name": "cqmgr:release-commit",
                    "value": identity["commit"],
                }
            ],
        },
        "specVersion": "1.6",
        "version": 1,
    }


def _distribution_names(version: str) -> tuple[str, str]:
    return (
        f"cqmgr-{version}-py3-none-any.whl",
        f"cqmgr-{version}.tar.gz",
    )


def _validate_bundle_identity(
    identity: Mapping[str, str],
    *,
    qualification: bool,
) -> None:
    mode = identity.get("mode")
    if mode == "pull-request":
        _validate_pull_request_bundle_identity(
            identity,
            qualification=qualification,
        )
    elif mode in {"dry-run", "release"}:
        _validate_release_bundle_identity(identity)
    else:
        msg = "candidate identity has an unsupported mode"
        raise ValueError(msg)
    if identity.get("repository") != CANONICAL_REPOSITORY:
        msg = "candidate identity does not name the canonical repository"
        raise ValueError(msg)
    if not COMMIT_PATTERN.fullmatch(identity.get("commit", "")):
        msg = "candidate identity requires one lowercase full commit SHA"
        raise ValueError(msg)
    if not identity.get("version"):
        msg = "candidate identity requires one version"
        raise ValueError(msg)


def _validate_pull_request_bundle_identity(
    identity: Mapping[str, str],
    *,
    qualification: bool,
) -> None:
    if not qualification:
        msg = "pull-request candidate identity is non-publishable"
        raise ValueError(msg)
    if set(identity) != PULL_REQUEST_CANDIDATE_IDENTITY_FIELDS:
        msg = "pull-request candidate identity has unexpected fields"
        raise ValueError(msg)
    pull_request = identity.get("pull_request", "")
    if (
        not pull_request.isdecimal()
        or pull_request.startswith("0")
        or int(pull_request) < 1
    ):
        msg = "pull-request candidate identity has an invalid number"
        raise ValueError(msg)


def _validate_release_bundle_identity(identity: Mapping[str, str]) -> None:
    if set(identity) != RELEASE_IDENTITY_FIELDS:
        msg = "release identity has unexpected fields"
        raise ValueError(msg)
    if identity.get("tag") != f"v{identity.get('version', '')}":
        msg = "release identity tag and version disagree"
        raise ValueError(msg)


def prepare_release_bundle(
    dist_dir: Path,
    output_dir: Path,
    identity: Mapping[str, str],
    requirements: str,
    *,
    qualification: bool = False,
) -> dict[str, object]:
    """Copy tested distributions once and produce release evidence around them."""
    _validate_bundle_identity(identity, qualification=qualification)
    version = identity["version"]
    distribution_names = _distribution_names(version)
    actual_distributions = tuple(
        sorted(
            path.name
            for path in dist_dir.iterdir()
            if path.name.endswith((".tar.gz", ".whl"))
        )
    )
    if actual_distributions != distribution_names:
        msg = "release candidate must contain exactly one sdist and one pure wheel"
        raise ValueError(msg)
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        msg = "release output directory must be empty"
        raise ValueError(msg)
    for name in distribution_names:
        shutil.copyfile(dist_dir / name, output_dir / name)

    sbom_name = SBOM_FILENAME_TEMPLATE.format(version=version)
    (output_dir / sbom_name).write_bytes(_canonical_json(_sbom(identity, requirements)))
    distributions = [
        {
            "name": name,
            "sha256": _sha256(output_dir / name),
            "size": (output_dir / name).stat().st_size,
        }
        for name in distribution_names
    ]
    manifest: dict[str, object] = {
        "commit": identity["commit"],
        "distributions": distributions,
        "publication": {
            "authorized": False,
            "requested": identity["mode"] == "release",
        },
        "repository": identity["repository"],
        "sbom": {
            "format": "CycloneDX",
            "name": sbom_name,
            "sha256": _sha256(output_dir / sbom_name),
            "spec_version": "1.6",
        },
        "schema": RELEASE_MANIFEST_SCHEMA,
        "version": version,
    }
    if identity["mode"] == "pull-request":
        manifest["pull_request"] = identity["pull_request"]
    else:
        manifest["tag"] = identity["tag"]
    manifest_path = output_dir / "release-manifest.json"
    manifest_path.write_bytes(_canonical_json(manifest))
    checksum_names = (*distribution_names, sbom_name, manifest_path.name)
    checksum_lines = [
        f"{_sha256(output_dir / name)}  {name}" for name in checksum_names
    ]
    (output_dir / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def _checksum_mapping(checksums_path: Path) -> dict[str, str]:
    text = checksums_path.read_text(encoding="utf-8")
    if not text.endswith("\n") or "\r" in text:
        msg = "checksum manifest must use canonical UTF-8 lines"
        raise ValueError(msg)
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        try:
            digest, name = line.split("  ", 1)
        except ValueError as error:
            msg = "checksum manifest has an invalid line"
            raise ValueError(msg) from error
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not name or name in checksums:
            if name in checksums:
                msg = f"checksum manifest has a duplicate entry for {name}"
                raise ValueError(msg)
            msg = "checksum manifest has an invalid digest or name"
            raise ValueError(msg)
        checksums[name] = digest
    return checksums


def verify_release_bundle(  # noqa: C901, PLR0912, PLR0915 - fail-closed bundle audit
    release_dir: Path,
    identity: Mapping[str, str],
    *,
    qualification: bool = False,
) -> dict[str, object]:
    """Verify exact assets, hashes, and identity without trusting job state."""
    _validate_bundle_identity(identity, qualification=qualification)
    version = identity["version"]
    sbom_name = SBOM_FILENAME_TEMPLATE.format(version=version)
    expected_hashed = {
        *_distribution_names(version),
        sbom_name,
        "release-manifest.json",
    }
    expected_assets = {*expected_hashed, "SHA256SUMS"}
    actual_assets = {path.name for path in release_dir.iterdir() if path.is_file()}
    if actual_assets != expected_assets:
        msg = "release bundle contains missing or unexpected assets"
        raise ValueError(msg)
    checksums = _checksum_mapping(release_dir / "SHA256SUMS")
    if set(checksums) != expected_hashed:
        msg = "checksum manifest does not cover the exact release assets"
        raise ValueError(msg)
    for name, digest in checksums.items():
        if _sha256(release_dir / name) != digest:
            msg = f"release checksum mismatch for {name}"
            raise ValueError(msg)
    manifest_value = json.loads(
        (release_dir / "release-manifest.json").read_text(encoding="utf-8")
    )
    if not isinstance(manifest_value, dict):
        msg = "release manifest must be an object"
        raise TypeError(msg)
    manifest = manifest_value
    expected_identity = {
        "commit": identity["commit"],
        "repository": identity["repository"],
        "version": version,
    }
    if identity["mode"] == "pull-request":
        expected_identity["pull_request"] = identity["pull_request"]
        expected_specific_fields = {"pull_request"}
    else:
        expected_identity["tag"] = identity["tag"]
        expected_specific_fields = {"tag"}
    if set(manifest).intersection({"pull_request", "tag"}) != expected_specific_fields:
        msg = "release manifest has conflicting identity fields"
        raise ValueError(msg)
    if any(manifest.get(key) != value for key, value in expected_identity.items()):
        msg = "release manifest identity does not match the qualified identity"
        raise ValueError(msg)
    if manifest.get("schema") != RELEASE_MANIFEST_SCHEMA:
        msg = "release manifest schema is unsupported"
        raise ValueError(msg)
    distributions = manifest.get("distributions")
    if not isinstance(distributions, list):
        msg = "release manifest distributions must be a list"
        raise TypeError(msg)
    if not all(isinstance(item, dict) for item in distributions):
        msg = "release manifest distribution entries must be objects"
        raise TypeError(msg)
    names = [item.get("name") for item in distributions]
    if len(set(names)) != len(names):
        msg = "release manifest distribution names must be unique"
        raise ValueError(msg)
    recorded = {item.get("name"): item.get("sha256") for item in distributions}
    expected_distribution_hashes = {
        name: checksums[name] for name in _distribution_names(version)
    }
    if recorded != expected_distribution_hashes:
        msg = "release manifest distribution checksums do not match exact bytes"
        raise ValueError(msg)
    sbom = manifest.get("sbom")
    if (
        not isinstance(sbom, dict)
        or sbom.get("name") != sbom_name
        or sbom.get("sha256") != checksums[sbom_name]
    ):
        msg = "release manifest SBOM checksum does not match exact bytes"
        raise ValueError(msg)
    return manifest


def verify_pull_request_candidate(  # noqa: PLR0913 - independent event facts
    release_dir: Path,
    identity: Mapping[str, str],
    project_path: Path,
    *,
    repository: str,
    pull_request_number: int,
    head_sha: str,
) -> dict[str, object]:
    """Independently bind candidate bytes to the consuming PR event."""
    expected_identity = pull_request_candidate_identity(
        project_path,
        repository=repository,
        pull_request_number=pull_request_number,
        head_sha=head_sha,
    )
    if dict(identity) != expected_identity:
        msg = "pull-request candidate does not match the consuming event identity"
        raise ValueError(msg)
    return verify_release_bundle(release_dir, identity, qualification=True)


def _write_json(path: Path | None, value: object) -> None:
    encoded = _canonical_json(value)
    if path is None:
        print(encoded.decode("utf-8"), end="")  # noqa: T201
    else:
        path.write_bytes(encoded)


def _identity_command(arguments: argparse.Namespace) -> None:
    identity = release_identity(
        arguments.project,
        repository=arguments.repository,
        ref_type=arguments.ref_type,
        ref_name=arguments.ref_name,
        commit_sha=arguments.commit,
        protected_main_sha=arguments.main_commit,
        dry_run=arguments.dry_run,
    )
    _write_json(arguments.output, identity)


def _pull_request_identity_command(arguments: argparse.Namespace) -> None:
    identity = pull_request_candidate_identity(
        arguments.project,
        repository=arguments.repository,
        pull_request_number=arguments.pull_request,
        head_sha=arguments.head_commit,
    )
    _write_json(arguments.output, identity)


def _load_identity(path: Path) -> dict[str, str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or set(value)
        not in (RELEASE_IDENTITY_FIELDS, PULL_REQUEST_CANDIDATE_IDENTITY_FIELDS)
        or any(not isinstance(item, str) or not item for item in value.values())
    ):
        msg = (
            "release or pull-request candidate identity must contain its exact "
            "required fields as strings"
        )
        raise ValueError(msg)
    identity = value
    _validate_bundle_identity(identity, qualification=True)
    return identity


def _prepare_command(arguments: argparse.Namespace) -> None:
    identity = _load_identity(arguments.identity)
    prepare_release_bundle(
        arguments.dist,
        arguments.output,
        identity,
        arguments.requirements.read_text(encoding="utf-8"),
        qualification=arguments.qualification,
    )


def _verify_command(arguments: argparse.Namespace) -> None:
    identity = _load_identity(arguments.identity)
    verify_release_bundle(arguments.release, identity)


def _verify_pull_request_command(arguments: argparse.Namespace) -> None:
    identity = _load_identity(arguments.identity)
    verify_pull_request_candidate(
        arguments.release,
        identity,
        arguments.project,
        repository=arguments.repository,
        pull_request_number=arguments.pull_request,
        head_sha=arguments.head_commit,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(required=True)
    identity = subparsers.add_parser("identity")
    identity.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    identity.add_argument("--repository", required=True)
    identity.add_argument("--ref-type", required=True)
    identity.add_argument("--ref-name", required=True)
    identity.add_argument("--commit", required=True)
    identity.add_argument("--main-commit", required=True)
    identity.add_argument("--dry-run", action="store_true")
    identity.add_argument("--output", type=Path)
    identity.set_defaults(handler=_identity_command)
    pull_request_identity = subparsers.add_parser("pull-request-identity")
    pull_request_identity.add_argument(
        "--project",
        type=Path,
        default=Path("pyproject.toml"),
    )
    pull_request_identity.add_argument("--repository", required=True)
    pull_request_identity.add_argument("--pull-request", type=int, required=True)
    pull_request_identity.add_argument("--head-commit", required=True)
    pull_request_identity.add_argument("--output", type=Path)
    pull_request_identity.set_defaults(handler=_pull_request_identity_command)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--dist", type=Path, required=True)
    prepare.add_argument("--identity", type=Path, required=True)
    prepare.add_argument("--requirements", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--qualification", action="store_true")
    prepare.set_defaults(handler=_prepare_command)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--release", type=Path, required=True)
    verify.add_argument("--identity", type=Path, required=True)
    verify.set_defaults(handler=_verify_command)
    verify_pull_request = subparsers.add_parser("verify-pull-request")
    verify_pull_request.add_argument("--release", type=Path, required=True)
    verify_pull_request.add_argument("--identity", type=Path, required=True)
    verify_pull_request.add_argument(
        "--project",
        type=Path,
        default=Path("pyproject.toml"),
    )
    verify_pull_request.add_argument("--repository", required=True)
    verify_pull_request.add_argument("--pull-request", type=int, required=True)
    verify_pull_request.add_argument("--head-commit", required=True)
    verify_pull_request.set_defaults(handler=_verify_pull_request_command)
    return parser


def main(arguments: Sequence[str] | None = None) -> None:
    """Run one release-qualification command."""
    parsed = _parser().parse_args(arguments)
    parsed.handler(parsed)


if __name__ == "__main__":
    main()
