from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


class _Navigation:
    def __init__(self, items: list[object], pages: list[object]) -> None:
        self.items = items
        self.pages = pages


class _PageItem:
    is_page = True
    is_section = False
    is_link = False

    def __init__(self, path: str) -> None:
        self.file = SimpleNamespace(src_uri=path)


class _SectionItem:
    is_page = False
    is_section = True
    is_link = False

    def __init__(self, title: str, children: list[object]) -> None:
        self.title = title
        self.children = children


def _load_navigation_hook(monkeypatch):
    mkdocs = ModuleType("mkdocs")
    mkdocs.__path__ = []
    structure = ModuleType("mkdocs.structure")
    structure.__path__ = []
    nav_module = ModuleType("mkdocs.structure.nav")
    nav_module.Navigation = _Navigation
    monkeypatch.setitem(sys.modules, "mkdocs", mkdocs)
    monkeypatch.setitem(sys.modules, "mkdocs.structure", structure)
    monkeypatch.setitem(sys.modules, "mkdocs.structure.nav", nav_module)

    root = Path(__file__).resolve().parents[1]
    hook_path = root / "project_tools" / "locale_navigation.py"
    spec = importlib.util.spec_from_file_location("tested_locale_navigation", hook_path)
    assert spec is not None and spec.loader is not None
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    return hook


def test_sidebar_and_search_pages_follow_the_current_locale(monkeypatch) -> None:
    hook = _load_navigation_hook(monkeypatch)
    english_item = _PageItem("jobs.md")
    chinese_item = _PageItem("jobs.zh-CN.md")
    navigation = _Navigation(
        [
            _SectionItem("English", [english_item]),
            _SectionItem("简体中文", [chinese_item]),
        ],
        [english_item, chinese_item],
    )

    english_context = {}
    hook.on_page_context(
        english_context,
        SimpleNamespace(file=SimpleNamespace(src_uri="index.md")),
        SimpleNamespace(theme=SimpleNamespace(language="en")),
        navigation,
    )
    assert english_context["nav"].items == [english_item]
    assert english_context["nav"].pages == [english_item]
    assert english_context["config"].theme.language == "en"

    chinese_context = {}
    hook.on_page_context(
        chinese_context,
        SimpleNamespace(file=SimpleNamespace(src_uri="index.zh-CN.md")),
        SimpleNamespace(theme=SimpleNamespace(language="en")),
        navigation,
    )
    assert chinese_context["nav"].items == [chinese_item]
    assert chinese_context["nav"].pages == [chinese_item]
    assert chinese_context["config"].theme.language == "zh"
