"""Exercise the release installation boundary with real offline wheel artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import typing
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="The installed live-read release job runs on Linux.",
)


def _release_step(name: str) -> str:
    workflow = typing.cast(
        "dict[str, typing.Any]",
        yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text()),
    )
    steps = workflow["jobs"]["installed-live-adapters"]["steps"]
    return next(
        typing.cast("str", step["run"]) for step in steps if step.get("name") == name
    )


def _wheel(
    directory: Path, name: str, version: str, *, candidate: bool = False
) -> Path:
    path = directory / f"{name}-{version}-py3-none-any.whl"
    info = f"{name}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{info}/METADATA",
            f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n"
            + ("Requires-Dist: qualification-dependency>=1\n" if candidate else ""),
        )
        archive.writestr(
            f"{info}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{info}/RECORD", "")
        archive.writestr(f"{name}.py", f"VERSION = {version!r}\n")
    return path


def _installation(
    tmp_path: Path, *, invalid: str | None = None
) -> subprocess.CompletedProcess[str]:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    dependency = _wheel(wheelhouse, "qualification_dependency", "1")
    _wheel(wheelhouse, "qualification_dependency", "2")
    digest = hashlib.sha256(dependency.read_bytes()).hexdigest()
    requirements = f"qualification-dependency==1 --hash=sha256:{digest}\n"
    if invalid == "tampered":
        with dependency.open("ab") as stream:
            stream.write(b"changed artifact bytes")
    elif invalid == "unhashed":
        requirements = "qualification-dependency==1\n"
    elif invalid == "incomplete":
        requirements = ""
    candidate = tmp_path / "candidate"
    release = candidate / "release"
    release.mkdir(parents=True)
    _wheel(release, "cqmgr", "0.1.0", candidate=True)
    (candidate / "release-requirements.txt").write_text(requirements)
    environment = dict(os.environ)
    environment.update(
        {
            "RUNNER_TEMP": str(tmp_path / "runner"),
            "VERSION": "0.1.0",
            "UV_CACHE_DIR": str(tmp_path / "cache"),
            "UV_FIND_LINKS": str(wheelhouse),
            "UV_NO_INDEX": "1",
            "UV_OFFLINE": "1",
        },
    )
    return subprocess.run(  # noqa: S603 - execute the repository's release step
        [
            "/bin/bash",
            "-c",
            _release_step("Install hash-verified candidate environment"),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_release_install_uses_locked_dependency_despite_newer_available_wheel(
    tmp_path: Path,
) -> None:
    """A newer compatible release cannot replace the hashed runtime closure."""
    completed = _installation(tmp_path)
    assert completed.returncode == 0, completed.stderr
    python = tmp_path / "runner" / "cqmgr-live" / "bin" / "python"
    installed = subprocess.run(  # noqa: S603 - the isolated installed Python
        [str(python), "-c", "import qualification_dependency as d; print(d.VERSION)"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert installed.stdout.strip() == "1"


@pytest.mark.parametrize("invalid", ["tampered", "unhashed", "incomplete"])
def test_release_install_rejects_unsafe_dependency_closure(
    tmp_path: Path,
    invalid: str,
) -> None:
    """Changed bytes, missing hashes, and missing transitive pins block auth."""
    completed = _installation(tmp_path, invalid=invalid)
    assert completed.returncode != 0
    assert "qualification-dependency" in completed.stderr


def test_changed_build_requirements_block_before_installation(tmp_path: Path) -> None:
    """Downloaded dependency evidence must match the checked-out committed lock."""
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(
        ROOT / "scripts" / "release_qualification.py",
        scripts / "release_qualification.py",
    )
    candidate = tmp_path / "candidate"
    release = candidate / "release"
    release.mkdir(parents=True)
    dist = tmp_path / "dist"
    dist.mkdir()
    _wheel(dist, "cqmgr", "0.1.0", candidate=True)
    (dist / "cqmgr-0.1.0.tar.gz").write_bytes(b"candidate sdist")
    identity = {
        "commit": "a" * 40,
        "mode": "dry-run",
        "repository": "nisavid/cqmgr",
        "tag": "v0.1.0",
        "version": "0.1.0",
    }
    identity_path = candidate / "release-identity.json"
    identity_path.write_text(json.dumps(identity))
    requirements = candidate / "release-requirements.txt"
    requirements.write_text("qualification-dependency==2\n")
    subprocess.run(  # noqa: S603 - prepare the candidate through its public CLI
        [
            sys.executable,
            str(scripts / "release_qualification.py"),
            "prepare",
            "--dist",
            str(dist),
            "--identity",
            str(identity_path),
            "--requirements",
            str(requirements),
            "--output",
            str(release),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    completed = subprocess.run(  # noqa: S603 - execute the release verification step
        [
            "/bin/bash",
            "-c",
            _release_step("Verify candidate and locked runtime resolution"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "differ" in completed.stdout
