"""The public site build: placeholders resolve and every page is self-contained."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD = REPO_ROOT / "marketing" / "build.py"


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the site once into a temporary directory."""
    out = tmp_path_factory.mktemp("site")
    result = subprocess.run(
        [sys.executable, str(BUILD), "--out", str(out)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return out


class TestSiteBuild:
    def test_expected_pages_exist(self, site: Path) -> None:
        assert (site / "index.html").exists()
        assert (site / "docs" / "index.html").exists()
        assert (site / ".nojekyll").exists()
        for doc in (REPO_ROOT / "docs").glob("*.md"):
            assert (site / "docs" / f"{doc.stem}.html").exists(), doc.name

    def test_no_unresolved_placeholders(self, site: Path) -> None:
        for page in site.rglob("*.html"):
            assert "{{" not in page.read_text(encoding="utf-8"), page.name

    def test_pages_inline_the_shared_stylesheet(self, site: Path) -> None:
        for page in site.rglob("*.html"):
            text = page.read_text(encoding="utf-8")
            assert "--ember:" in text, page.name
            assert '<link rel="stylesheet"' not in text, page.name

    def test_landing_quotes_config_defaults(self, site: Path) -> None:
        import config

        page = (site / "index.html").read_text(encoding="utf-8")
        assert f"{int(config.MAX_POSITION_WEIGHT_PCT)}%" in page
        assert f"{config.RECOMMENDATION_EXPIRY_DAYS} days" in page
        assert f"at {config.WEEKLY_RUN_HOUR:02d}:00" in page

    def test_referenced_assets_are_shipped(self, site: Path) -> None:
        page = (site / "index.html").read_text(encoding="utf-8")
        for name in ("favicon.svg", "og-image.png", "cash-ash-hero.jpg"):
            assert f"assets/{name}" in page
            assert (site / "assets" / name).exists(), name

    def test_fonts_resolve_from_every_page(self, site: Path) -> None:
        for page in site.rglob("*.html"):
            text = page.read_text(encoding="utf-8")
            for url in re.findall(r'url\("([^"]+\.woff2)"\)', text):
                assert (page.parent / url).exists(), f"{page.name}: {url}"

    def test_docs_md_links_are_rewritten(self, site: Path) -> None:
        for page in (site / "docs").glob("*.html"):
            assert '.md"' not in page.read_text(encoding="utf-8"), page.name
