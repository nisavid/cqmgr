"""Exact-version guard for dependency-license review exceptions."""

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


def test_reviewed_license_exceptions_match_the_lock() -> None:
    """The committed exceptions describe exactly the current locked artifacts."""
    result = _verify(ROOT / "uv.lock")

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_reviewed_license_exception_rejects_a_version_change(tmp_path: Path) -> None:
    """A dependency update fails until its license metadata is reviewed again."""
    current = (ROOT / "uv.lock").read_text()
    changed = current.replace(
        'name = "protobuf"\nversion = "7.35.1"',
        'name = "protobuf"\nversion = "8.0.0"',
        1,
    )
    assert changed != current
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(changed)

    result = _verify(lock_path)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "review the new artifact" in result.stderr
