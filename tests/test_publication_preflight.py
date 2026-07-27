"""Fail-closed publication retry contracts."""

from __future__ import annotations

import hashlib
import json
import runpy
from email.message import Message
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "publication_preflight.py"
VERSION = "0.1.0"


def _module() -> dict[str, Any]:
    return runpy.run_path(str(SCRIPT))


def _candidate(tmp_path: Path) -> Path:
    candidate = tmp_path / "release"
    candidate.mkdir()
    assets = {
        f"cqmgr-{VERSION}-py3-none-any.whl": b"wheel",
        f"cqmgr-{VERSION}.tar.gz": b"sdist",
        f"cqmgr-{VERSION}.cdx.json": b"sbom",
        "release-manifest.json": b"",
    }
    distributions = [
        {
            "name": name,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for name, content in assets.items()
        if name.endswith((".whl", ".tar.gz"))
    ]
    manifest = {
        "distributions": distributions,
        "version": VERSION,
    }
    assets["release-manifest.json"] = (
        json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    for name, content in assets.items():
        (candidate / name).write_bytes(content)
    checksums = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {name}\n"
        for name, content in assets.items()
    )
    (candidate / "SHA256SUMS").write_text(
        checksums,
        encoding="utf-8",
        newline="\n",
    )
    return candidate


def test_absent_pypi_version_requires_upload(tmp_path: Path) -> None:
    """A genuinely absent version is the only state that starts PyPI upload."""
    check_pypi = cast("Any", _module()["check_pypi"])

    assert check_pypi(_candidate(tmp_path), VERSION, None) == "publish"


@pytest.mark.parametrize(
    "mismatch",
    ["asset-bytes", "duplicate-entry", "missing-entry", "missing-newline"],
)
def test_candidate_sha256sums_mismatch_fails_before_publication(
    tmp_path: Path,
    mismatch: str,
) -> None:
    """Every candidate asset must agree with one exact SHA256SUMS entry."""
    check_pypi = cast("Any", _module()["check_pypi"])
    candidate = _candidate(tmp_path)
    checksums_path = candidate / "SHA256SUMS"
    lines = checksums_path.read_text(encoding="utf-8").splitlines()
    if mismatch == "asset-bytes":
        (candidate / f"cqmgr-{VERSION}.cdx.json").write_bytes(b"replacement")
    elif mismatch == "duplicate-entry":
        lines.append(lines[0])
        checksums_path.write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    elif mismatch == "missing-entry":
        checksums_path.write_text(
            "\n".join(lines[:-1]) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    else:
        checksums_path.write_text(
            "\n".join(lines),
            encoding="utf-8",
            newline="\n",
        )

    with pytest.raises(ValueError, match="SHA256SUMS"):
        check_pypi(candidate, VERSION, None)


def test_exact_pypi_version_skips_upload(tmp_path: Path) -> None:
    """A retry skips PyPI only when both immutable distributions already match."""
    check_pypi = cast("Any", _module()["check_pypi"])
    candidate = _candidate(tmp_path)
    urls = [
        {
            "filename": path.name,
            "digests": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
        }
        for path in candidate.iterdir()
        if path.name.endswith((".whl", ".tar.gz"))
    ]

    assert (
        check_pypi(candidate, VERSION, {"info": {"version": VERSION}, "urls": urls})
        == "skip"
    )


@pytest.mark.parametrize("mismatch", ["filename", "sha256", "version"])
def test_pypi_mismatch_fails_closed(tmp_path: Path, mismatch: str) -> None:
    """An occupied PyPI version can never be overwritten or silently skipped."""
    check_pypi = cast("Any", _module()["check_pypi"])
    candidate = _candidate(tmp_path)
    distributions = [
        path for path in candidate.iterdir() if path.name.endswith((".whl", ".tar.gz"))
    ]
    urls = [
        {
            "filename": path.name,
            "digests": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
        }
        for path in distributions
    ]
    metadata = {"info": {"version": VERSION}, "urls": urls}
    if mismatch == "filename":
        urls[0]["filename"] = "other.whl"
    elif mismatch == "sha256":
        urls[0]["digests"]["sha256"] = "0" * 64
    else:
        metadata["info"]["version"] = "9.9.9"

    with pytest.raises(ValueError, match="PyPI"):
        check_pypi(candidate, VERSION, metadata)


def test_absent_github_release_requires_creation(tmp_path: Path) -> None:
    """A missing tag release is the only state that starts release creation."""
    check_github_release = cast("Any", _module()["check_github_release"])

    assert (
        check_github_release(
            _candidate(tmp_path),
            f"v{VERSION}",
            None,
            lambda _: b"",
        )
        == "publish"
    )


def test_exact_github_release_skips_creation_after_comparing_every_asset(
    tmp_path: Path,
) -> None:
    """A retry downloads and byte-compares the complete immutable asset set."""
    check_github_release = cast("Any", _module()["check_github_release"])
    candidate = _candidate(tmp_path)
    assets = [
        {
            "name": path.name,
            "size": path.stat().st_size,
            "url": f"https://api.github.test/assets/{index}",
        }
        for index, path in enumerate(sorted(candidate.iterdir()))
    ]
    remote = {
        asset["url"]: (candidate / asset["name"]).read_bytes() for asset in assets
    }
    downloaded: list[str] = []

    def load(url: str) -> bytes:
        downloaded.append(url)
        return remote[url]

    result = check_github_release(
        candidate,
        f"v{VERSION}",
        {
            "assets": assets,
            "draft": False,
            "tag_name": f"v{VERSION}",
        },
        load,
    )

    assert result == "skip"
    assert downloaded == [asset["url"] for asset in assets]


@pytest.mark.parametrize("mismatch", ["asset-set", "bytes", "draft", "size", "tag"])
def test_github_release_mismatch_fails_closed(
    tmp_path: Path,
    mismatch: str,
) -> None:
    """Existing releases are immutable; any disagreement requires intervention."""
    check_github_release = cast("Any", _module()["check_github_release"])
    candidate = _candidate(tmp_path)
    assets = [
        {
            "name": path.name,
            "size": path.stat().st_size,
            "url": f"https://api.github.test/assets/{index}",
        }
        for index, path in enumerate(sorted(candidate.iterdir()))
    ]
    remote = {
        asset["url"]: (candidate / asset["name"]).read_bytes() for asset in assets
    }
    metadata = {
        "assets": assets,
        "draft": False,
        "tag_name": f"v{VERSION}",
    }
    if mismatch == "asset-set":
        assets.pop()
    elif mismatch == "bytes":
        remote[assets[0]["url"]] = b"replacement"
    elif mismatch == "draft":
        metadata["draft"] = True
    elif mismatch == "size":
        assets[0]["size"] += 1
    else:
        metadata["tag_name"] = "v9.9.9"

    with pytest.raises(ValueError, match="GitHub Release"):
        check_github_release(
            candidate,
            f"v{VERSION}",
            metadata,
            remote.__getitem__,
        )


def test_remote_error_is_not_treated_as_absent_publication_state() -> None:
    """Only a remote 404 permits publication; server failures stop the workflow."""
    module = _module()
    fetch_json = cast("Any", module["_fetch_json"])

    def fail(*_args: object, **_kwargs: object) -> None:
        raise HTTPError(
            url="https://pypi.org/pypi/cqmgr/0.1.0/json",
            code=500,
            msg="server error",
            hdrs=Message(),
            fp=None,
        )

    fetch_json.__globals__["urlopen"] = fail

    with pytest.raises(RuntimeError, match="HTTP 500"):
        fetch_json("https://pypi.org/pypi/cqmgr/0.1.0/json", headers={})
