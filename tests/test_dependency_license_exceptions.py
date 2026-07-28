"""Exact-version guard for dependency-license review exceptions."""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
VERIFIER = ROOT / "scripts" / "verify_dependency_license_exceptions.py"


def _verify(lock_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(VERIFIER), str(lock_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _with_protobuf_version(lock: str, version: str) -> str:
    changed, replacements = re.subn(
        r'(\[\[package\]\]\nname = "protobuf"\nversion = ")[^"]+',
        rf"\g<1>{version}",
        lock,
        count=1,
    )
    assert replacements == 1
    return changed


def test_reviewed_license_exceptions_match_the_lock() -> None:
    """The committed exceptions describe exactly the current locked artifacts."""
    result = _verify(ROOT / "uv.lock")

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_reviewed_license_exception_rejects_a_version_change(tmp_path: Path) -> None:
    """A dependency update fails until its license metadata is reviewed again."""
    current = (ROOT / "uv.lock").read_text()
    changed = _with_protobuf_version(current, "8.0.0")
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(changed)

    result = _verify(lock_path)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "review the new artifact" in result.stderr
