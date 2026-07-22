"""Verified owner-only Windows ACL enforcement for private local state."""

from __future__ import annotations

import base64
import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_WINDOWS_PRIVATE_ACL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$target = $env:CQMGR_ACL_TARGET
try {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
} catch {
    exit 12
}
$inheritance = ''
try {
    $isDirectory = Test-Path -LiteralPath $target -PathType Container
} catch {
    exit 16
}
if ($isDirectory) {
    $inheritance = 'OICI'
}
$sddl = "D:P(A;${inheritance};FA;;;$($identity.Value))"
try {
    if ($isDirectory) {
        $acl = New-Object System.Security.AccessControl.DirectorySecurity
    } else {
        $acl = New-Object System.Security.AccessControl.FileSecurity
    }
    $acl.SetSecurityDescriptorSddlForm(
        $sddl,
        [System.Security.AccessControl.AccessControlSections]::Access
    )
} catch {
    exit 13
}
try {
    if ($isDirectory) {
        [System.IO.Directory]::SetAccessControl($target, $acl)
    } else {
        [System.IO.File]::SetAccessControl($target, $acl)
    }
} catch {
    exit 14
}
try {
    if ($isDirectory) {
        $verifiedAcl = [System.IO.Directory]::GetAccessControl(
            $target,
            [System.Security.AccessControl.AccessControlSections]::Access
        )
    } else {
        $verifiedAcl = [System.IO.File]::GetAccessControl(
            $target,
            [System.Security.AccessControl.AccessControlSections]::Access
        )
    }
} catch {
    exit 15
}
$verified = @($verifiedAcl.Access)
if (-not $verifiedAcl.AreAccessRulesProtected) {
    exit 21
}
if ($verified.Count -ne 1) {
    exit 22
}
try {
    $verifiedIdentity = $verified[0].IdentityReference.Translate(
        [System.Security.Principal.SecurityIdentifier]
    )
} catch {
    exit 23
}
if ($verifiedIdentity.Value -ne $identity.Value) {
    exit 23
}
if (
    $verified[0].AccessControlType -ne
    [System.Security.AccessControl.AccessControlType]::Allow
) {
    exit 24
}
$fullControl = [int][System.Security.AccessControl.FileSystemRights]::FullControl
$actualRights = [int]$verified[0].FileSystemRights
if (($actualRights -band $fullControl) -ne $fullControl) {
    exit 25
}
"""


def restrict_windows_acl(path: Path) -> None:
    """Replace and verify one Windows path ACL with active-account full control."""
    if os.name != "nt":
        return
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    executable = rf"{system_root}\System32\WindowsPowerShell\v1.0\powershell.exe"
    environment = os.environ.copy()
    environment["CQMGR_ACL_TARGET"] = str(path)
    encoded_command = base64.b64encode(
        _WINDOWS_PRIVATE_ACL_SCRIPT.encode("utf-16le")
    ).decode("ascii")
    returncode = -1
    try:
        returncode = subprocess.run(  # noqa: S603
            [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded_command,
            ],
            check=False,
            capture_output=True,
            env=environment,
            timeout=10,
        ).returncode
    except subprocess.TimeoutExpired as error:
        msg = "Windows private ACL enforcement timed out"
        raise OSError(msg) from error
    if returncode != 0:
        msg = f"Windows private ACL enforcement failed ({returncode})"
        raise OSError(msg)
