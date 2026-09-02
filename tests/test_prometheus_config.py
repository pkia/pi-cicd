"""prometheus/prometheus.yml is the source of truth for scraping — keep it honest.

Fails if a scrape job goes missing, a target points off the box, the
scrape interval drifts, or docs/prometheus.md stops naming the jobs it
actually scrapes. Docs that map the running system need a test, or they rot.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "prometheus" / "prometheus.yml"
DOC = REPO / "docs" / "prometheus.md"

EXPECTED_JOBS = ("prometheus", "node")


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
