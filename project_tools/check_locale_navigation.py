"""Check that built English and Chinese pages use locale-specific sidebars."""

from pathlib import Path

SITE = Path(__file__).resolve().parents[1] / "site"


def page_document(path: str) -> str:
    return (SITE / path).read_text(encoding="utf-8")


def primary_navigation(path: str) -> str:
    html = page_document(path)
    return html.split("md-sidebar--primary", 1)[1].split("md-sidebar--secondary", 1)[0]


def main() -> None:
    english_document = page_document("index.html")
    chinese_document = page_document("index.zh-CN/index.html")
    english = primary_navigation("index.html")
    chinese = primary_navigation("index.zh-CN/index.html")

    assert '<html lang="en"' in english_document
    assert '<html lang="zh"' in chinese_document

    for label in (
        "Backtest jobs and results",
        "Development and checks",
        "Operations and troubleshooting",
        "Repository ownership",
    ):
        assert label in english
    assert "简体中文" not in english
    assert "回测任务与结果" not in english

    for label in ("回测任务与结果", "开发与检查", "发布与故障排查", "仓库职责与迁移"):
        assert label in chinese
    assert "English" not in chinese
    assert "Backtest jobs and results" not in chinese


if __name__ == "__main__":
    main()
