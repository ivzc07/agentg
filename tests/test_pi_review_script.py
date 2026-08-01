"""`scripts/pi-review` must stay runnable on a fresh Windows worktree.

The repo's merge gate is `scripts/pi-review`, so the script breaking is not a
cosmetic problem - it blocks every PR. Two Windows-specific traps have bitten
it before, and both are regressions a test can catch:

1. Git for Windows sets `core.autocrlf=true` in its SYSTEM gitconfig, so every
   clone and `git worktree add` rewrote the script to CRLF. A CRLF shebang makes
   the kernel search for an interpreter named `sh\r`. `.gitattributes` pins the
   script to LF; these tests assert that pin is still in force.
2. `bash` on the PATH can resolve to WSL, which cannot read a linked worktree's
   Windows-path `.git` file. The script guards against that up front.
"""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pi-review"


def test_script_exists():
    assert SCRIPT.is_file(), "the merge gate script is missing"


def test_gitattributes_pins_the_script_to_lf():
    """Ask git itself, not the filesystem.

    Checking the bytes on disk would pass trivially on Linux CI (where
    autocrlf is off) even with the pin deleted - i.e. a test that cannot
    fail where it runs. `git check-attr` reports the configured attribute
    on every platform, so deleting or weakening the `.gitattributes` entry
    fails this test in CI too.
    """
    result = subprocess.run(
        ["git", "check-attr", "eol", "--", "scripts/pi-review"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip().endswith(": eol: lf"), (
        "scripts/pi-review is no longer pinned to LF in .gitattributes; "
        "Git for Windows' core.autocrlf=true will rewrite it to CRLF on "
        f"checkout and break the shebang (got: {result.stdout.strip()!r})"
    )


def test_script_has_no_crlf_on_disk():
    """The symptom itself, on whatever platform the suite is running."""
    raw = SCRIPT.read_bytes()
    assert b"\r\n" not in raw, (
        "scripts/pi-review contains CRLF line endings; its shebang will not "
        "resolve under non-MSYS shells"
    )


def test_shebang_is_intact():
    first = SCRIPT.read_bytes().split(b"\n", 1)[0]
    assert first == b"#!/usr/bin/env sh", f"unexpected shebang: {first!r}"


def test_script_parses_as_posix_sh():
    """A syntax error in the gate would only surface at PR time otherwise."""
    if not (shell := _find_sh()):
        pytest.skip("no POSIX sh available")
    result = subprocess.run([shell, "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, f"scripts/pi-review is not valid sh: {result.stderr}"


def test_guards_against_wsl_bash():
    """WSL cannot read a linked worktree, so the script must refuse to run
    there rather than emit a confusing `not a git repository` from git."""
    if not (shell := _find_sh()):
        pytest.skip("no POSIX sh available")
    result = subprocess.run(
        [shell, str(SCRIPT), "999"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "WSL_DISTRO_NAME": "Ubuntu"},
    )
    assert result.returncode != 0, "the script ran under WSL instead of refusing"
    assert "WSL" in result.stderr, (
        f"expected a WSL-specific diagnostic, got: {result.stderr!r}"
    )


def _find_sh() -> str | None:
    from shutil import which

    return which("sh") or which("bash")
