"""Fail closed when retrying immutable PyPI and GitHub Release publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

PROJECT = "cqmgr"
PYPI_BASE_URL = "https://pypi.org/pypi"
GITHUB_API_BASE_URL = "https://api.github.com"
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
NETWORK_TIMEOUT_SECONDS = 30


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _object(value: object, *, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        msg = f"{context} must be an object"
        raise TypeError(msg)
    return value


def _checksum_mapping(checksums_path: Path) -> dict[str, str]:
    text = checksums_path.read_text(encoding="utf-8")
    if not text.endswith("\n") or "\r" in text:
        msg = "SHA256SUMS must use canonical UTF-8 lines"
        raise ValueError(msg)
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        try:
            digest, name = line.split("  ", 1)
        except ValueError as error:
            msg = "SHA256SUMS contains an invalid line"
            raise ValueError(msg) from error
        if SHA256_PATTERN.fullmatch(digest) is None or not name or name in checksums:
            msg = "SHA256SUMS contains an invalid or duplicate asset"
            raise ValueError(msg)
        checksums[name] = digest
    return checksums


def _candidate_details(  # noqa: C901 - one fail-closed candidate audit
    candidate_dir: Path,
    version: str,
) -> tuple[dict[str, Path], dict[str, str]]:
    manifest = _object(
        json.loads(
            (candidate_dir / "release-manifest.json").read_text(encoding="utf-8")
        ),
        context="release manifest",
    )
    if manifest.get("version") != version:
        msg = "release candidate version does not match the publication version"
        raise ValueError(msg)
    distribution_values = manifest.get("distributions")
    if not isinstance(distribution_values, list):
        msg = "release candidate distributions must be a list"
        raise TypeError(msg)
    expected_distribution_names = {
        f"{PROJECT}-{version}-py3-none-any.whl",
        f"{PROJECT}-{version}.tar.gz",
    }
    distributions: dict[str, str] = {}
    for value in distribution_values:
        item = _object(value, context="release candidate distribution")
        name = item.get("name")
        digest = item.get("sha256")
        if (
            not isinstance(name, str)
            or name in distributions
            or not isinstance(digest, str)
            or SHA256_PATTERN.fullmatch(digest) is None
        ):
            msg = "release candidate distribution identity is invalid or duplicated"
            raise ValueError(msg)
        distributions[name] = digest
    if set(distributions) != expected_distribution_names:
        msg = "release candidate does not contain the exact PyPI distributions"
        raise ValueError(msg)

    expected_asset_names = {
        *expected_distribution_names,
        f"{PROJECT}-{version}.cdx.json",
        "release-manifest.json",
        "SHA256SUMS",
    }
    paths = tuple(candidate_dir.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in paths):
        msg = "release candidate assets must be direct regular files"
        raise ValueError(msg)
    assets = {path.name: path for path in paths}
    if set(assets) != expected_asset_names:
        msg = "release candidate contains missing or unexpected assets"
        raise ValueError(msg)
    checksums = _checksum_mapping(assets["SHA256SUMS"])
    expected_checksum_names = expected_asset_names - {"SHA256SUMS"}
    if set(checksums) != expected_checksum_names:
        msg = "SHA256SUMS does not cover the exact candidate assets"
        raise ValueError(msg)
    for name, digest in checksums.items():
        if _sha256(assets[name]) != digest:
            msg = f"SHA256SUMS does not match candidate asset bytes: {name}"
            raise ValueError(msg)
    if any(checksums[name] != digest for name, digest in distributions.items()):
        msg = "SHA256SUMS does not match the release manifest distributions"
        raise ValueError(msg)
    return assets, distributions


def check_pypi(
    candidate_dir: Path,
    version: str,
    metadata: Mapping[str, Any] | None,
) -> str:
    """Return publish for an absent version or skip for one exact remote version."""
    _, distributions = _candidate_details(candidate_dir, version)
    if metadata is None:
        return "publish"
    info = _object(metadata.get("info"), context="PyPI release info")
    if info.get("version") != version:
        msg = "PyPI version identity does not match the immutable candidate"
        raise ValueError(msg)
    url_values = metadata.get("urls")
    if not isinstance(url_values, list):
        msg = "PyPI release files must be a list"
        raise TypeError(msg)
    remote: dict[str, str] = {}
    for value in url_values:
        item = _object(value, context="PyPI release file")
        filename = item.get("filename")
        digests = _object(item.get("digests"), context="PyPI release file digests")
        digest = digests.get("sha256")
        if (
            not isinstance(filename, str)
            or filename in remote
            or not isinstance(digest, str)
        ):
            msg = "PyPI release filenames or SHA-256 values are invalid or duplicated"
            raise ValueError(msg)
        remote[filename] = digest
    if remote != distributions:
        msg = "PyPI release filenames or SHA-256 values do not match the candidate"
        raise ValueError(msg)
    return "skip"


def check_github_release(
    candidate_dir: Path,
    tag: str,
    metadata: Mapping[str, Any] | None,
    download_asset: Callable[[str], bytes],
) -> str:
    """Return publish for no release or skip after exact remote byte comparison."""
    if not tag.startswith("v") or len(tag) == 1:
        msg = "GitHub Release tag must be a non-empty version tag"
        raise ValueError(msg)
    version = tag.removeprefix("v")
    assets, _ = _candidate_details(candidate_dir, version)
    if metadata is None:
        return "publish"
    if metadata.get("tag_name") != tag or metadata.get("draft") is not False:
        msg = "GitHub Release tag or published state does not match the candidate"
        raise ValueError(msg)
    asset_values = metadata.get("assets")
    if not isinstance(asset_values, list):
        msg = "GitHub Release assets must be a list"
        raise TypeError(msg)
    remote: dict[str, tuple[int, str]] = {}
    for value in asset_values:
        item = _object(value, context="GitHub Release asset")
        name = item.get("name")
        size = item.get("size")
        url = item.get("url")
        if (
            not isinstance(name, str)
            or name in remote
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(url, str)
        ):
            msg = "GitHub Release asset metadata is invalid or duplicated"
            raise ValueError(msg)
        remote[name] = (size, url)
    if set(remote) != set(assets):
        msg = "GitHub Release asset set does not match the immutable candidate"
        raise ValueError(msg)
    for name in sorted(assets):
        local = assets[name].read_bytes()
        size, url = remote[name]
        if size != len(local) or download_asset(url) != local:
            msg = f"GitHub Release asset bytes do not match the candidate: {name}"
            raise ValueError(msg)
    return "skip"


def _fetch_json(
    url: str,
    *,
    headers: Mapping[str, str],
) -> Mapping[str, Any] | None:
    request = Request(url, headers=dict(headers))  # noqa: S310 - fixed HTTPS APIs
    try:
        with urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:  # noqa: S310
            value = json.loads(response.read())
    except HTTPError as error:
        if error.code == 404:  # noqa: PLR2004 - HTTP not-found has fixed semantics
            return None
        msg = f"remote publication preflight failed with HTTP {error.code}"
        raise RuntimeError(msg) from error
    return _object(value, context="remote publication metadata")


def _pypi_metadata(version: str) -> Mapping[str, Any] | None:
    return _fetch_json(
        f"{PYPI_BASE_URL}/{PROJECT}/{quote(version, safe='')}/json",
        headers={"Accept": "application/json"},
    )


def _github_metadata(
    repository: str,
    tag: str,
    token: str,
) -> Mapping[str, Any] | None:
    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        msg = "GitHub repository must be an owner/name pair"
        raise ValueError(msg)
    return _fetch_json(
        f"{GITHUB_API_BASE_URL}/repos/{repository}/releases/tags/{quote(tag, safe='')}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _github_asset_loader(repository: str, token: str) -> Callable[[str], bytes]:
    expected_prefix = f"{GITHUB_API_BASE_URL}/repos/{repository}/releases/assets/"

    def download(url: str) -> bytes:
        if not url.startswith(expected_prefix):
            msg = "GitHub Release asset URL is outside the canonical repository API"
            raise ValueError(msg)
        request = Request(  # noqa: S310 - URL is restricted above
            url,
            headers={
                "Accept": "application/octet-stream",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(  # noqa: S310 - URL is restricted above
                request,
                timeout=NETWORK_TIMEOUT_SECONDS,
            ) as response:
                return response.read()
        except HTTPError as error:
            msg = f"GitHub Release asset download failed with HTTP {error.code}"
            raise RuntimeError(msg) from error

    return download


def _write_action(output: Path, action: str) -> None:
    if action not in {"publish", "skip"}:
        msg = "publication preflight returned an unsupported action"
        raise ValueError(msg)
    with output.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"publish={'true' if action == 'publish' else 'false'}\n")


def _pypi_command(arguments: argparse.Namespace) -> None:
    action = check_pypi(
        arguments.candidate,
        arguments.version,
        _pypi_metadata(arguments.version),
    )
    _write_action(arguments.github_output, action)


def _github_command(arguments: argparse.Namespace) -> None:
    token = os.environ.get(arguments.token_environment)
    if not token:
        msg = f"{arguments.token_environment} must contain a GitHub API token"
        raise ValueError(msg)
    metadata = _github_metadata(arguments.repository, arguments.tag, token)
    action = check_github_release(
        arguments.candidate,
        arguments.tag,
        metadata,
        _github_asset_loader(arguments.repository, token),
    )
    _write_action(arguments.github_output, action)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(required=True)
    pypi = subparsers.add_parser("pypi")
    pypi.add_argument("--candidate", type=Path, required=True)
    pypi.add_argument("--version", required=True)
    pypi.add_argument("--github-output", type=Path, required=True)
    pypi.set_defaults(handler=_pypi_command)
    github = subparsers.add_parser("github")
    github.add_argument("--candidate", type=Path, required=True)
    github.add_argument("--repository", required=True)
    github.add_argument("--tag", required=True)
    github.add_argument("--token-environment", default="GITHUB_TOKEN")
    github.add_argument("--github-output", type=Path, required=True)
    github.set_defaults(handler=_github_command)
    return parser


def main(arguments: Sequence[str] | None = None) -> None:
    """Inspect immutable remote publication state without overwriting it."""
    parsed = _parser().parse_args(arguments)
    parsed.handler(parsed)


if __name__ == "__main__":
    main()
