"""The action-pin gate: a moving ref in a workflow must be a named fault.

Everything here drives the real `action-pins` CLI in a subprocess against
fixture repos under tmp_path — the tool's own output is the evidence, not a
restatement of its classifier in Python.

The three "how YAML reads it" cases carry the most weight, because each one
is a way a grep-shaped checker passes the wrong tree: a commented-out step is
not a step; a quoted value is the value; and a `uses:` line inside a
`run: |` block is shell text, not an action.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "action-pins"

PINNED = "11bd71901bbe5b1630ceea73d27597364c9af683"
OTHER = "a" * 40


def write_workflow(root, name="ci.yml", text=""):
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    path = wf / name
    path.write_text(text)
    return path


def run(root, *extra, env=None):
    cmd = [sys.executable, str(TOOL), "--dir", str(root), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def steps(*uses_lines):
    """A workflow whose steps are the given `uses:` lines, in order."""
    body = "\n".join(f"      - {line}" for line in uses_lines)
    return f"name: CI\non: push\njobs:\n  check:\n    runs-on: ubuntu-latest\n    steps:\n{body}\n"


def test_all_pinned_exits_zero_listing_each_action(tmp_path):
    write_workflow(
        tmp_path,
        text=steps(
            f"uses: actions/checkout@{PINNED}",
            f"uses: actions/setup-python@{OTHER}",
            "uses: ./.github/actions/local-thing",
        ),
    )
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "actions/checkout" in out and "actions/setup-python" in out
    assert "sha" in out and "local" in out
    assert out.count("actions/checkout") == 1
    assert "3 action reference(s) in 1 workflow file(s)" in out
    assert "OK — every action reference is pinned" in out


def test_major_tag_faults_naming_file_line_and_action(tmp_path):
    write_workflow(tmp_path, text=steps(f"uses: actions/checkout@{PINNED}",
                                        "uses: actions/cache@v4"))
    r = run(tmp_path)
    assert r.returncode == 1
    # the fault line for the second step: header is 6 lines, step 1 at 7, step 2 at 8
    assert "FAIL .github/workflows/ci.yml:8" in r.stderr, r.stderr
    assert "actions/cache" in r.stderr and "v4" in r.stderr and "tag" in r.stderr
    assert "actions/checkout" not in r.stderr  # the pinned one is not a fault
    assert "1 moving ref(s); 1 pinned" in r.stderr
    assert "OK —" not in r.stdout


def test_commented_out_step_is_not_a_step(tmp_path):
    write_workflow(
        tmp_path,
        text=steps(
            f"uses: actions/checkout@{PINNED}",
            "# - uses: actions/checkout@v4   # pinned later",
            "  # uses: actions/upload-artifact@v4",
        ),
    )
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "1 action reference(s)" in r.stdout
    assert "upload-artifact" not in r.stdout


def test_quoted_value_pins_what_the_unquoted_spelling_pins(tmp_path):
    write_workflow(tmp_path, text=steps('uses: "actions/checkout@v4"'))
    r = run(tmp_path)
    assert r.returncode == 1
    assert "actions/checkout" in r.stderr and "v4" in r.stderr
    assert "tag" in r.stderr


def test_uses_inside_a_block_scalar_is_shell_text(tmp_path):
    text = (
        "name: CI\non: push\njobs:\n  check:\n    runs-on: ubuntu-latest\n"
        "    steps:\n"
        f"      - uses: actions/checkout@{PINNED}\n"
        "      - name: echo a uses line\n"
        "        run: |\n"
        "          echo 'uses: actions/evil@v1'\n"
        "          cat <<'EOF'\n"
        "          uses: actions/evil-2@main\n"
        "          EOF\n"
    )
    write_workflow(tmp_path, text=text)
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "1 action reference(s)" in r.stdout
    assert "evil" not in r.stdout


def test_branch_ref_is_named_a_branch(tmp_path):
    write_workflow(tmp_path, text=steps("uses: actions/checkout@main"))
    r = run(tmp_path)
    assert r.returncode == 1
    assert "branch" in r.stderr and "checkout" in r.stderr


def test_local_action_is_not_a_moving_ref(tmp_path):
    write_workflow(tmp_path, text=steps("uses: ./.github/actions/lint"))
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "local" in r.stdout


def test_trailing_comment_is_not_part_of_the_ref(tmp_path):
    write_workflow(tmp_path, text=steps("uses: actions/cache@v4  # bump later"))
    r = run(tmp_path)
    assert r.returncode == 1
    assert "'v4'" in r.stdout or "cache@v4" in r.stdout
    assert "later" not in r.stdout


def test_docker_digest_passes_and_docker_tag_faults(tmp_path):
    digest = "sha256:" + "b" * 64
    write_workflow(tmp_path, text=steps(f"uses: docker://alpine@{digest}"))
    ok = run(tmp_path)
    assert ok.returncode == 0, ok.stderr
    assert "digest" in ok.stdout

    write_workflow(tmp_path, text=steps("uses: docker://alpine:3"))
    bad = run(tmp_path)
    assert bad.returncode == 1
    assert "alpine" in bad.stderr and "tag" in bad.stderr


def test_value_on_the_following_line_is_read(tmp_path):
    text = (
        "name: CI\non: push\njobs:\n  check:\n    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses:\n"
        "          actions/checkout@v4\n"
    )
    write_workflow(tmp_path, text=text)
    r = run(tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ci.yml:7" in r.stderr and "actions/checkout" in r.stderr


def test_flow_mapping_step_is_read(tmp_path):
    write_workflow(tmp_path, text=steps("{uses: actions/checkout@v4}"))
    r = run(tmp_path)
    assert r.returncode == 1
    assert "ci.yml:7" in r.stderr and "checkout" in r.stderr


def test_no_workflows_is_unmeasurable_not_a_pass(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 2
    assert "nothing measured" in r.stderr


def test_missing_dir_is_unmeasurable(tmp_path):
    r = run(tmp_path / "nope")
    assert r.returncode == 2


def test_json_owns_stdout_on_success_and_failure(tmp_path):
    write_workflow(tmp_path, text=steps(f"uses: actions/checkout@{PINNED}"))
    clean = run(tmp_path, "--json")
    assert clean.returncode == 0
    data = json.loads(clean.stdout)  # alone: no human line alongside on success
    assert data["ok"] is True and data["pinned"] == 1 and data["moving"] == 0
    assert data["refs"][0]["type"] == "sha"

    write_workflow(tmp_path, text=steps("uses: actions/checkout@v4"))
    bad = run(tmp_path, "--json")
    assert bad.returncode == 1
    data = json.loads(bad.stdout)
    assert data["ok"] is False and data["moving"] == 1
    assert data["refs"][0]["file"] == ".github/workflows/ci.yml"
    assert data["refs"][0]["line"] == 7
    assert data["refs"][0]["pinned"] is False


def test_read_only_by_source_scan():
    src = TOOL.read_text()
    for banned in ("write_text", "write_bytes", "unlink", "os.remove", "shutil",
                   "import subprocess", "subprocess.", "mkdir", "rename"):
        assert banned not in src, f"{banned} appears in a tool that must write nothing"


def test_tree_is_byte_identical_after_a_run(tmp_path):
    write_workflow(tmp_path, text=steps("uses: actions/checkout@v4"))
    write_workflow(tmp_path, name="other.yaml", text=steps(f"uses: x/y@{PINNED}"))

    def snapshot():
        out = {}
        for p in sorted(tmp_path.rglob("*")):
            if p.is_file():
                st = p.stat()
                out[str(p.relative_to(tmp_path))] = (
                    st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()
                )
        return out

    before = snapshot()
    assert run(tmp_path).returncode == 1
    assert run(tmp_path, "--json").returncode == 1
    assert snapshot() == before


def test_runs_with_the_runner_path(tmp_path):
    """No sibling tools are shelled out to — PATH stripped must change nothing."""
    write_workflow(tmp_path, text=steps(f"uses: actions/checkout@{PINNED}"))
    env = {**os.environ, "PATH": "/usr/bin:/bin"}
    r = run(tmp_path, env=env)
    assert r.returncode == 0, r.stderr


def test_this_repo_is_pinned():
    """The live gate: pi-cicd's own workflows pass it, refs listed as sha."""
    r = subprocess.run([sys.executable, str(TOOL)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "sha" in r.stdout
    assert "OK — every action reference is pinned" in r.stdout
