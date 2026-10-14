"""Show only the navigation matching the current document language."""

from __future__ import annotations

from copy import copy, deepcopy

from mkdocs.structure.nav import Navigation

_MESSAGES = {
    "en-US": {
        "theme.to_dark": "Switch to dark mode",
        "theme.to_light": "Switch to light mode",
    },
    "zh-CN": {
        "theme.to_dark": "切换至深色模式",
        "theme.to_light": "切换至浅色模式",
    },
}


def _localized_palette(theme, chinese: bool):
    palette = deepcopy(theme.get("palette") or [])
    messages = _MESSAGES["zh-CN" if chinese else "en-US"]
    keys = {"default": "theme.to_dark", "slate": "theme.to_light"}
    for item in palette:
        key = keys.get(item.get("scheme"))
        if key and item.get("toggle"):
            item["toggle"]["name"] = messages.get(key, _MESSAGES["en-US"][key])
    return palette


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
    localized_config = copy(config)
    localized_theme = copy(config.theme)
    localized_theme.language = "zh" if chinese else "en"
    localized_theme.palette = _localized_palette(config.theme, chinese)
    localized_config.theme = localized_theme
    context["config"] = localized_config
    return context
