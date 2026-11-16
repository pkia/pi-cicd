"""install.sh and this file read ONE list of repo-root tools, not two copies.

History: the 2026-09-03 find was pipeline-check and pi-doctor never
symlinked by install.sh, so they were unusable from an interactive shell.
The fix at the time was a TOOLS list here and a hand-written ln block
there — two copies of the same facts, which drifted again on 2026-11-16
(the dns-check ship touched both, one entry was still missing, and CI went
red for a list edit rather than a code defect).

The fix now: tools.manifest is the only list. install.sh links from it,
chmods from it, and refuses to run when the repo root holds a tool the
manifest omits — and these tests drive the installer's own --check-tools
instead of restating the rule in Python, so the two readers cannot
disagree about what a "tool" is.

The scan rule, stated once: a tool is an extensionless regular file at
the repo root that starts with a shebang. Libraries carry .py,
install.sh carries .sh, the manifest is data — no hand-kept ignore list.
"""
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = (REPO / "install.sh").read_text()
MANIFEST = REPO / "tools.manifest"

IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", ".pytest_cache", ".ruff_cache"
)


def manifest_names(path=MANIFEST):
    names = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def repo_root_tools(root=REPO):
    """The installer's scan rule, mirrored here so the test can state it."""
    found = set()
    for p in root.iterdir():
        if not p.is_file() or "." in p.name:
            continue
        with p.open("rb") as fh:
            if fh.read(2) == b"#!":
                found.add(p.name)
    return found


def run_check_tools(repo_dir, home):
    return subprocess.run(
        ["bash", "install.sh", "--check-tools"],
        cwd=repo_dir,
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=120,
    )


def stub_system_calls(tmp_path):
    """A PATH dir whose sudo/gh are no-ops, so the full installer is safe.

    install.sh's systemd and /etc steps are the owner's gated pass; a test
    of the linking half must not reach them. sudo is stubbed to succeed
    without doing anything and gh to "not authenticated", which keeps the
    run hermetic and offline.
    """
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    (bindir / "sudo").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "gh").write_text("#!/bin/sh\nexit 1\n")
    for name in ("sudo", "gh"):
        (bindir / name).chmod(0o755)
    return bindir


def copy_repo(tmp_path):
    dst = tmp_path / "pi-cicd"
    shutil.copytree(REPO, dst, ignore=IGNORE)
    home = tmp_path / "home"
    home.mkdir()
    return dst, home


def test_manifest_lists_only_real_tools():
    names = manifest_names()
    assert names, "tools.manifest lists no tools"
    assert len(names) == len(set(names)), f"duplicate entries: {names}"
    for name in names:
        path = REPO / name
        assert path.is_file(), f"manifest lists {name!r} but {path} is not a file"
        with path.open("rb") as fh:
            assert fh.read(2) == b"#!", f"{name} does not start with a shebang"


def test_every_repo_root_tool_is_in_the_manifest():
    listed = set(manifest_names())
    present = repo_root_tools()
    missing = sorted(present - listed)
    assert not missing, (
        f"repo root holds tool(s) the manifest omits: {missing}. A tool that "
        "is not in tools.manifest is never linked onto PATH; add each name."
    )
    ghosts = sorted(listed - present)
    assert not ghosts, f"manifest lists tool(s) not at the repo root: {ghosts}"


def test_installer_links_from_the_manifest_not_a_hardcoded_list():
    # No per-tool ln line may survive: every name would be a second copy.
    for name in manifest_names():
        assert f'"$REPO_DIR/{name}" "$BIN_DIR/{name}"' not in INSTALL_SH, (
            f"install.sh still hardcodes a link for {name}"
        )
    assert "tools.manifest" in INSTALL_SH, "install.sh does not read the manifest"
    assert '"$REPO_DIR/$tool" "$BIN_DIR/$tool"' in INSTALL_SH, (
        "install.sh no longer links the manifest's names in a loop"
    )
    # The chmod list drifted separately from the ln list once already.
    assert 'chmod +x "$REPO_DIR/$tool"' in INSTALL_SH, (
        "install.sh should mark the manifest's tools executable, not a second list"
    )


def test_current_tree_passes_check_tools():
    """Acceptance: the current tree passes unchanged."""
    result = subprocess.run(
        ["bash", "install.sh", "--check-tools"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert f"{len(manifest_names())} tool(s) listed" in result.stdout
    assert "no tool at the repo root is unlisted" in result.stdout


def test_check_tools_changes_nothing(tmp_path):
    repo, home = copy_repo(tmp_path)

    def snapshot():
        return sorted(
            (str(p.relative_to(repo)), p.stat().st_size, p.stat().st_mode,
             p.stat().st_mtime_ns)
            for p in repo.rglob("*")
        )

    before = snapshot()
    result = run_check_tools(repo, home)
    assert result.returncode == 0, result.stderr
    assert snapshot() == before, "--check-tools modified the tree"
    # It must also exit before mkdir -p "$HOME/.local/bin".
    assert not (home / ".local").exists(), "--check-tools created state under HOME"


def test_deleting_a_manifest_entry_fails_the_installer_naming_the_tool(tmp_path):
    """Acceptance: delete an entry -> the installer run (and the test) fail."""
    repo, home = copy_repo(tmp_path)
    doomed = "feed-fresh"
    lines = (repo / "tools.manifest").read_text().splitlines()
    (repo / "tools.manifest").write_text(
        "\n".join(l for l in lines if l.strip() != doomed) + "\n"
    )

    result = run_check_tools(repo, home)
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert doomed in output, f"the fault must name {doomed}: {output}"
    # And the test half sees it too: the tree scan and the manifest disagree.
    assert doomed in repo_root_tools(repo)
    assert doomed not in manifest_names(repo / "tools.manifest")


def test_stub_script_at_repo_root_fails_until_the_manifest_lists_it(tmp_path):
    """Acceptance: a new stub fails the gate; one added line clears it."""
    repo, home = copy_repo(tmp_path)
    stub = repo / "drill-stub-tool"
    stub.write_text("#!/usr/bin/env python3\nprint('stub')\n")
    stub.chmod(0o644)  # a fresh file is not executable yet — the scan must see it

    failed = run_check_tools(repo, home)
    assert failed.returncode != 0
    assert "drill-stub-tool" in (failed.stdout + failed.stderr)

    with (repo / "tools.manifest").open("a") as fh:
        fh.write("drill-stub-tool\n")
    passed = run_check_tools(repo, home)
    assert passed.returncode == 0, passed.stderr


def test_manifest_entry_with_no_tool_behind_it_fails(tmp_path):
    repo, home = copy_repo(tmp_path)
    with (repo / "tools.manifest").open("a") as fh:
        fh.write("ghost-tool\n")
    result = run_check_tools(repo, home)
    assert result.returncode != 0
    assert "ghost-tool" in (result.stdout + result.stderr)


def test_full_install_links_every_manifest_tool(tmp_path):
    """The old promise, kept behaviourally: every listed tool reaches PATH."""
    repo, home = copy_repo(tmp_path)
    bindir = stub_system_calls(tmp_path)
    result = subprocess.run(
        ["bash", "install.sh"],
        cwd=repo,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{bindir}:/usr/local/bin:/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    linked = home / ".local" / "bin"
    for name in manifest_names(repo / "tools.manifest"):
        target = linked / name
        assert target.is_symlink(), f"{name} was not linked onto PATH"
        assert target.resolve() == (repo / name).resolve()
        assert os.access(repo / name, os.X_OK), f"{name} is not executable"
    assert len(sorted(linked.iterdir())) == len(manifest_names())


def test_duplicate_manifest_entry_fails(tmp_path):
    repo, home = copy_repo(tmp_path)
    first = manifest_names(repo / "tools.manifest")[0]
    with (repo / "tools.manifest").open("a") as fh:
        fh.write(first + "\n")
    result = run_check_tools(repo, home)
    assert result.returncode != 0
    assert first in (result.stdout + result.stderr)
