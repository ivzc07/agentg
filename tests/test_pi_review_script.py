"""`scripts/pi-review` must stay runnable on a fresh Windows worktree.

The repo's merge gate is `scripts/pi-review`, so the script breaking is not a
cosmetic problem - it blocks every PR. Two Windows-specific traps have bitten it
before: CRLF line endings breaking the shebang, and `bash` on the PATH resolving
to WSL, which cannot read a linked worktree. These tests hold both closed.

Why each trap happens and why the fix is shaped the way it is, is documented
once in `docs/agents/pr-merges.md` ("Windows environment traps") and in the
`.gitattributes` comment block - deliberately not restated here, so the policy
has one place to change rather than three.
"""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pi-review"


def test_script_exists():
    assert SCRIPT.is_file(), "the merge gate script is missing"


@pytest.mark.parametrize("path", ["scripts/pi-review", "tools/example.sh"])
def test_gitattributes_pins_the_script_to_lf(path):
    """Ask git itself, not the filesystem.

    Checking the bytes on disk would pass trivially on Linux CI (where
    autocrlf is off) even with the pin deleted - i.e. a test that cannot
    fail where it runs. `git check-attr` reports the configured attribute
    on every platform, so deleting or weakening the `.gitattributes` entry
    fails this test in CI too.
    """
    result = subprocess.run(
        ["git", "check-attr", "eol", "--", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip().endswith(": eol: lf"), (
        f"{path} is no longer pinned to LF in .gitattributes; "
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


# Real /proc/version strings. The guard must fire on the WSL kernels and stay
# silent on the Git Bash ones - firing under Git Bash would break the merge gate
# on every Windows machine, which is the more dangerous direction of the two.
WSL_KERNELS = [
    "Linux version 5.15.167.4-microsoft-standard-WSL2 (root@build) #1 SMP",
    "Linux version 4.4.0-19041-Microsoft (Microsoft@Microsoft.com) #1237-Microsoft",
    # Exercises the `wsl` half of the probe's alternation. A custom-built WSL2
    # kernel need not carry "microsoft" in its version string, and without this
    # fixture deleting `|wsl` from the probe would pass the whole suite.
    "Linux version 6.6.36-WSL2-custom (builder@local) #1 SMP PREEMPT_DYNAMIC",
]
NON_WSL_KERNELS = [
    "MINGW64_NT-10.0-26200 version 3.6.5-22c95533.x86_64 (@runnervm) 2025-10-10",
    "MSYS_NT-10.0-19045 version 3.4.6.x86_64 (@build) 2023-12-01",
    "Linux version 6.8.0-1014-azure (buildd@lcy02) #16-Ubuntu SMP",
]


STUB_GH_MARKER = "stub gh: no network in tests"


def _run_guard(tmp_path, kernel, **env):
    """Run the script with a stubbed /proc/version, via the script's test seam.

    Reading the machine's real /proc/version instead would make these tests
    assert whatever the host happens to be: the negative case would pass on
    Linux CI even with the probe widened to match Git Bash, i.e. it could not
    fail where it runs. Feeding known kernel strings makes both directions
    meaningful on every platform.

    `gh` is stubbed so that when the guard correctly stays silent the script
    cannot reach the network. Without the stub, a host with an authenticated
    `gh` on PATH would let this test launch a real review against PR 999.
    """
    shell = _find_sh()
    if not shell:
        pytest.skip("no POSIX sh available")
    proc_version = tmp_path / "proc_version"
    proc_version.write_text(kernel + "\n")
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub_gh = stub_bin / "gh"
    stub_gh.write_text(f'#!/usr/bin/env sh\necho "{STUB_GH_MARKER}" >&2\nexit 1\n')
    stub_gh.chmod(0o755)
    return subprocess.run(
        [shell, str(SCRIPT), "999"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": f"{stub_bin}:/usr/bin:/bin",
            "PI_REVIEW_PROC_VERSION": str(proc_version),
            **env,
        },
    )


@pytest.mark.parametrize("kernel", WSL_KERNELS)
def test_guards_against_wsl_bash(tmp_path, kernel):
    """WSL cannot read a linked worktree, so the script must refuse to run
    there rather than emit a confusing `not a git repository` from git."""
    result = _run_guard(tmp_path, kernel)
    assert result.returncode != 0, "the script ran under WSL instead of refusing"
    assert "WSL" in result.stderr, (
        f"expected a WSL-specific diagnostic, got: {result.stderr!r}"
    )


def test_wsl_distro_name_alone_trips_the_guard(tmp_path):
    """WSL is still WSL even if /proc/version is unreadable."""
    result = _run_guard(tmp_path, NON_WSL_KERNELS[0], WSL_DISTRO_NAME="Ubuntu")
    assert result.returncode != 0
    assert "WSL" in result.stderr


@pytest.mark.parametrize("kernel", NON_WSL_KERNELS)
def test_guard_stays_silent_off_wsl(tmp_path, kernel):
    """The dangerous direction: a probe that over-matches would refuse to run
    on every Windows machine and take the merge gate down with it."""
    result = _run_guard(tmp_path, kernel)
    assert "WSL" not in result.stderr, (
        f"the WSL guard fired on a non-WSL kernel ({kernel!r}); this would "
        f"break the gate everywhere. stderr: {result.stderr!r}"
    )
    # Positively confirm the script ran *past* the guard rather than dying
    # early for some unrelated reason, which would make the assertion above
    # true for the wrong reason.
    assert STUB_GH_MARKER in result.stderr, (
        f"expected the script to proceed to `gh` after the guard stayed "
        f"silent, got: {result.stderr!r}"
    )


def _find_sh() -> str | None:
    """A POSIX shell that is not WSL.

    A bare `which("bash")` can resolve to C:\\Windows\\System32\\bash.exe - the
    WSL launcher this script exists to guard against. WSL cannot open the
    script via its Windows path, so using it here would fail these tests
    spuriously rather than skip them.
    """
    from shutil import which

    for name in ("sh", "bash"):
        found = which(name)
        if found and "system32" not in found.lower():
            return found
    return None
