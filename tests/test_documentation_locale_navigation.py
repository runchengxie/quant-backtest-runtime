from __future__ import annotations

import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path


class _PrimaryNavigationText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._nav_states: list[bool | None] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "nav":
            return
        classes = (dict(attrs).get("class") or "").split()
        if not self._nav_states:
            self._nav_states.append("md-nav--primary" in classes)
        else:
            parent_is_primary = all(state is True for state in self._nav_states)
            self._nav_states.append(
                parent_is_primary and "md-nav--secondary" not in classes
            )

    def handle_endtag(self, tag: str) -> None:
        if tag == "nav" and self._nav_states:
            self._nav_states.pop()

    def handle_data(self, data: str) -> None:
        if self._nav_states and self._nav_states[-1] is True:
            self.text.append(data.strip())


def test_rendered_sidebars_follow_the_page_locale(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    site_dir = tmp_path / "site"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mkdocs",
            "build",
            "--strict",
            "--site-dir",
            str(site_dir),
        ],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    pages = {
        "English": site_dir / "index.html",
        "Chinese": site_dir / "index.zh-CN/index.html",
    }
    navigation: dict[str, str] = {}
    for locale, path in pages.items():
        parser = _PrimaryNavigationText()
        parser.feed(path.read_text(encoding="utf-8"))
        navigation[locale] = " ".join(parser.text)

    assert "Backtest jobs and results" in navigation["English"]
    assert "回测任务与结果" not in navigation["English"]
    assert "回测任务与结果" in navigation["Chinese"]
    assert "Backtest jobs and results" not in navigation["Chinese"]
