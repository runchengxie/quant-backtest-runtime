"""Show only the navigation matching the current document language."""

from __future__ import annotations

from copy import copy

from mkdocs.structure.nav import Navigation


def _is_chinese(page) -> bool:
    return page.file.src_uri.endswith(".zh-CN.md")


def _for_locale(items, chinese: bool):
    selected = []
    for item in items:
        if item.is_page:
            if _is_chinese(item) == chinese:
                selected.append(item)
        elif item.is_section:
            children = _for_locale(item.children, chinese)
            if children:
                section = copy(item)
                section.children = children
                selected.append(section)
        elif item.is_link:
            selected.append(item)
    return selected


def on_page_context(context, page, config, nav):
    chinese = _is_chinese(page)
    items = _for_locale(nav.items, chinese)
    flattened = []
    for item in items:
        if item.is_section and item.title in {"English", "简体中文"}:
            flattened.extend(item.children)
        else:
            flattened.append(item)
    pages = [item for item in nav.pages if _is_chinese(item) == chinese]
    context["nav"] = Navigation(flattened, pages)
    return context
