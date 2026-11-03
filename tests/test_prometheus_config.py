"""prometheus/prometheus.yml is the source of truth for scraping — keep it honest.

Fails if a scrape job goes missing, a target points off the box, the
scrape interval drifts, the cluster job loses its file-based bearer
token (never inline), or docs/prometheus.md stops naming the jobs it
actually scrapes. Docs that map the running system need a test, or they rot.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "prometheus" / "prometheus.yml"
DOC = REPO / "docs" / "prometheus.md"

EXPECTED_JOBS = ("prometheus", "node", "ntfy", "cluster")
CLUSTER_TOKEN_PATH = "/etc/cluster-metrics.token"


def test_config_has_expected_jobs():
    text = CONFIG.read_text()
    for job in EXPECTED_JOBS:
        assert f"job_name: {job}" in text


def test_all_targets_are_loopback():
    text = CONFIG.read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("targets:"):
            assert "127.0.0.1" in stripped, f"off-box scrape target: {line}"


def test_scrape_interval_is_sane():
    assert "scrape_interval: 15s" in CONFIG.read_text()


def test_docs_name_the_scraped_jobs():
    doc = DOC.read_text()
    for job in EXPECTED_JOBS:
        assert f"`{job}`" in doc


def test_cluster_job_token_is_file_based_not_inline():
    """The bearer credential is a file reference, never a value in the repo."""
    text = CONFIG.read_text()
    assert f"credentials_file: {CLUSTER_TOKEN_PATH}" in text
    assert "credentials:" not in text, "inline credentials in the scrape config"


def test_no_secret_looking_value_in_config():
    import re
    text = CONFIG.read_text()
    assert not re.search(r"[0-9a-f]{32,}", text), "hex secret-looking string in the yml"


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not installed")
def test_promtool_accepts_config_once_the_token_file_exists(tmp_path):
    """promtool validates credential files at load, so the shipped yml is
    checked with the token path substituted by a real file — the missing
    piece live is exactly the owner's token install, nothing else."""
    token_file = tmp_path / "cluster-metrics.token"
    token_file.write_text("drill-substitute-token\n")
    staged = tmp_path / "prometheus.yml"
    staged.write_text(CONFIG.read_text().replace(CLUSTER_TOKEN_PATH, str(token_file)))
    res = subprocess.run(["promtool", "check", "config", str(staged)],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "cluster" in staged.read_text()

