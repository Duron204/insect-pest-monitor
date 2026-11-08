# -*- coding: utf-8 -*-
"""把 LLM 生成的诊断报告（Markdown-lite 文本）渲染成 Word 文档。

不引入 markdown → docx 库：报告的格式约定很小（二级标题 / 列表 /
加粗 / 引用编号），手写 60 行解析足够，还能保证与聊天页
`aiMarkdown.ts` 的分块规则一致 —— 两边对"同一份文本"的理解不会漂移。
"""
from __future__ import annotations

import io
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

__all__ = ["report_to_docx"]

_ACCENT = RGBColor(0x2F, 0x9E, 0x5F)     # 苔绿主色（Word 里没有 CSS 变量，取同值）
_MUTED = RGBColor(0x6B, 0x7F, 0x72)
_INLINE = re.compile(r"\*\*([^*\n]+)\*\*|\[(\d{1,2})\]")


def _add_runs(paragraph, line: str) -> None:
    """一行文本 → runs：**加粗** 与 [n] 引用编号单独成 run。"""
    last = 0
    for match in _INLINE.finditer(line):
        if match.start() > last:
            paragraph.add_run(line[last : match.start()])
        if match.group(1) is not None:
            run = paragraph.add_run(match.group(1))
            run.bold = True
        else:
            run = paragraph.add_run(f"[{match.group(2)}]")
            run.font.color.rgb = _ACCENT
            run.bold = True
        last = match.end()
    if last < len(line):
        paragraph.add_run(line[last:])


def report_to_docx(title: str, content: str, meta: str = "") -> bytes:
    """生成 .docx 字节流。meta 是"范围/生成时间/token"之类的一行说明。"""
    document = Document()

    heading = document.add_heading(title, level=0)
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in heading.runs:
        run.font.color.rgb = RGBColor(0x17, 0x23, 0x1B)

    if meta:
        meta_para = document.add_paragraph()
        meta_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = meta_para.add_run(meta)
        run.font.size = Pt(9)
        run.font.color.rgb = _MUTED

    normalized = (content or "").replace("\r\n", "\n").strip()
    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            document.add_heading(line[3:], level=2)
        elif line.startswith("# "):
            document.add_heading(line[2:], level=1)
        elif re.match(r"^[-•·]\s+", line) or re.match(r"^\d{1,2}[.、)]\s*", line):
            text = re.sub(r"^([-•·]|\d{1,2}[.、)])\s*", "", line, count=1)
            _add_runs(document.add_paragraph(style="List Bullet"), text)
        else:
            _add_runs(document.add_paragraph(), line)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
