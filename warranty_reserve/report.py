"""终端报告排版与 JSON 输出。"""
from __future__ import annotations

from .timeutil import yuan_format


def money(minor) -> str:
    if minor is None:
        return "—"
    return f"{yuan_format(minor)} 元"


def _width(text: str) -> int:
    return sum(2 if ord(char) > 127 else 1 for char in text)


def table(headers: list[str], rows: list[list[str]], aligns: list[str] | None = None) -> str:
    widths = [_width(str(header)) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], _width(str(cell)))
    aligns = aligns or ["l"] * len(headers)
    lines = []
    lines.append("  ".join(_pad(str(header), widths[index], "l") for index, header in enumerate(headers)))
    lines.append("  ".join("-" * widths[index] for index in range(len(headers))))
    for row in rows:
        lines.append(
            "  ".join(_pad(str(cell), widths[index], aligns[index]) for index, cell in enumerate(row))
        )
    return "\n".join(lines)


def _pad(text: str, width: int, align: str) -> str:
    # 中文按 2 列宽粗略对齐。
    padding = max(0, width - _width(text))
    if align == "r":
        return " " * padding + text
    return text + " " * padding


def section(title: str) -> str:
    return f"\n## {title}"


def bullet(text: str) -> str:
    return f"- {text}"
