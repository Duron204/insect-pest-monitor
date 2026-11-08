# -*- coding: utf-8 -*-
"""检测报告导出：xlsx / docx / html。

报告包含：任务元信息、虫数汇总（框数 vs 去重虫数）、分类计数、
命中虫种的植保知识、标注图，以及框级明细（前若干行）。
生成位置在任务产物目录，随任务一起删除。
"""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from ..models import Task
from .storage import task_dir

# 明细写入报告的行数上限，避免报告变成几十万行
MAX_DETAIL_ROWS = 500

_LEVEL_LABEL = {"info": "提示", "warn": "警告", "critical": "严重"}


def _task_meta_rows(task: Task, counts: dict[str, int]) -> list[tuple[str, str]]:
    return [
        ("任务编号", str(task.id)),
        ("检测模式", task.mode),
        ("来源", task.source_name or "-"),
        ("状态", task.status),
        ("置信度阈值", f"{task.conf:.2f}"),
        ("IOU 阈值", f"{task.iou:.2f}"),
        ("开始时间", task.started_at.strftime("%Y-%m-%d %H:%M:%S") if task.started_at else "-"),
        ("结束时间", task.finished_at.strftime("%Y-%m-%d %H:%M:%S") if task.finished_at else "-"),
        ("耗时(ms)", str(task.duration_ms) if task.duration_ms is not None else "-"),
        ("处理帧数", str(task.frame_count)),
        ("检出框总数", str(task.total_objects)),
        ("去重虫数", str(task.unique_objects)),
        ("涉及虫种数", str(len(counts))),
    ]


def _report_basename(task: Task) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"report_{task.id:06d}_{stamp}"


# ---------------------------------------------------------------- xlsx
def build_xlsx(
    task: Task,
    settings: Any,
    counts: dict[str, int],
    detections: Sequence[Any],
    species: dict[str, dict[str, Any]],
    annotated_path: Path | None,
) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()

    # --- 概览 ---
    sheet = workbook.active
    sheet.title = "检测概览"
    title_font = Font(size=14, bold=True)
    head_fill = PatternFill("solid", fgColor="D9EAD3")

    sheet["A1"] = "病虫害检测报告"
    sheet["A1"].font = title_font
    row = 3
    for label, value in _task_meta_rows(task, counts):
        sheet.cell(row=row, column=1, value=label).font = Font(bold=True)
        sheet.cell(row=row, column=2, value=value)
        row += 1

    row += 1
    sheet.cell(row=row, column=1, value="分类计数").font = title_font
    row += 1
    for column, header in enumerate(("虫种", "数量", "占比"), start=1):
        cell = sheet.cell(row=row, column=column, value=header)
        cell.font = Font(bold=True)
        cell.fill = head_fill
    total = sum(counts.values()) or 1
    for name, count in sorted(counts.items(), key=lambda item: -item[1]):
        row += 1
        sheet.cell(row=row, column=1, value=name)
        sheet.cell(row=row, column=2, value=count)
        sheet.cell(row=row, column=3, value=f"{count / total * 100:.1f}%")

    for column, width in zip("ABC", (24, 12, 10)):
        sheet.column_dimensions[column].width = width

    # --- 植保知识 ---
    if species:
        knowledge = workbook.create_sheet("植保知识")
        headers = ("虫种", "寄主作物", "危害特征", "防治建议", "防治参考阈值")
        for column, header in enumerate(headers, start=1):
            cell = knowledge.cell(row=1, column=column, value=header)
            cell.font = Font(bold=True)
            cell.fill = head_fill
        for index, (name, item) in enumerate(species.items(), start=2):
            knowledge.cell(row=index, column=1, value=name)
            knowledge.cell(row=index, column=2, value=item.get("host_crops") or "")
            knowledge.cell(row=index, column=3, value=item.get("damage") or "")
            knowledge.cell(row=index, column=4, value=item.get("control") or "")
            knowledge.cell(row=index, column=5, value=item.get("threshold_desc") or "")
        for column, width in zip("ABCDE", (16, 28, 50, 60, 30)):
            knowledge.column_dimensions[column].width = width
        for row_index in range(2, len(species) + 2):
            for column in range(1, 6):
                knowledge.cell(row=row_index, column=column).alignment = Alignment(
                    wrap_text=True, vertical="top"
                )

    # --- 明细 ---
    if detections:
        detail = workbook.create_sheet("检测明细")
        headers = ("序号", "帧号", "类别", "中文名", "置信度", "x1", "y1", "x2", "y2", "轨迹ID")
        for column, header in enumerate(headers, start=1):
            cell = detail.cell(row=1, column=column, value=header)
            cell.font = Font(bold=True)
            cell.fill = head_fill
        for index, det in enumerate(detections[:MAX_DETAIL_ROWS], start=2):
            detail.cell(row=index, column=1, value=index - 1)
            detail.cell(row=index, column=2, value=getattr(det, "frame_index", 1))
            detail.cell(row=index, column=3, value=getattr(det, "class_name", ""))
            detail.cell(row=index, column=4, value=getattr(det, "zh_name", "") or "")
            detail.cell(row=index, column=5, value=round(float(getattr(det, "score", 0.0)), 4))
            detail.cell(row=index, column=6, value=getattr(det, "x1", 0))
            detail.cell(row=index, column=7, value=getattr(det, "y1", 0))
            detail.cell(row=index, column=8, value=getattr(det, "x2", 0))
            detail.cell(row=index, column=9, value=getattr(det, "y2", 0))
            detail.cell(row=index, column=10, value=getattr(det, "track_id", None))
        for column in range(1, len(headers) + 1):
            detail.column_dimensions[get_column_letter(column)].width = 12

    # --- 标注图 ---
    if annotated_path is not None and annotated_path.exists():
        try:
            from openpyxl.drawing.image import Image as XLImage

            sheet.add_image(XLImage(str(annotated_path)), "E3")
        except Exception:  # noqa: BLE001 - 图片嵌入失败不应导致报告失败
            pass

    target = task_dir(settings, task.id) / f"{_report_basename(task)}.xlsx"
    workbook.save(str(target))
    return target


# ---------------------------------------------------------------- docx
def build_docx(
    task: Task,
    settings: Any,
    counts: dict[str, int],
    detections: Sequence[Any],
    species: dict[str, dict[str, Any]],
    annotated_path: Path | None,
) -> Path:
    from docx import Document
    from docx.shared import Inches, Pt

    document = Document()
    heading = document.add_heading("病虫害检测报告", level=1)
    heading.alignment = 1

    table = document.add_table(rows=0, cols=2)
    table.style = "Light Grid Accent 1"
    for label, value in _task_meta_rows(task, counts):
        cells = table.add_row().cells
        cells[0].text = label
        cells[1].text = value

    document.add_paragraph()
    document.add_heading("分类计数", level=2)
    count_table = document.add_table(rows=1, cols=3)
    count_table.style = "Light Grid Accent 1"
    for cell, text in zip(count_table.rows[0].cells, ("虫种", "数量", "占比")):
        cell.text = text
    total = sum(counts.values()) or 1
    for name, count in sorted(counts.items(), key=lambda item: -item[1]):
        cells = count_table.add_row().cells
        cells[0].text = name
        cells[1].text = str(count)
        cells[2].text = f"{count / total * 100:.1f}%"

    if annotated_path is not None and annotated_path.exists():
        document.add_paragraph()
        document.add_heading("标注图", level=2)
        try:
            document.add_picture(str(annotated_path), width=Inches(5.8))
        except Exception:  # noqa: BLE001
            pass

    if species:
        document.add_heading("植保知识", level=2)
        for name, item in species.items():
            document.add_heading(name, level=3)
            for label, key in (
                ("寄主作物", "host_crops"),
                ("危害特征", "damage"),
                ("防治建议", "control"),
                ("防治参考阈值", "threshold_desc"),
            ):
                paragraph = document.add_paragraph()
                run = paragraph.add_run(f"{label}：")
                run.bold = True
                run.font.size = Pt(10.5)
                paragraph.add_run(item.get(key) or "-")

    if detections:
        document.add_heading("检测明细（前 %d 条）" % min(len(detections), MAX_DETAIL_ROWS), level=2)
        detail = document.add_table(rows=1, cols=6)
        detail.style = "Light Grid Accent 1"
        for cell, text in zip(detail.rows[0].cells, ("帧号", "类别", "置信度", "x1", "y1", "x2/y2")):
            cell.text = text
        for det in detections[:MAX_DETAIL_ROWS]:
            cells = detail.add_row().cells
            cells[0].text = str(getattr(det, "frame_index", 1))
            cells[1].text = f"{getattr(det, 'class_name', '')}/{getattr(det, 'zh_name', '') or ''}"
            cells[2].text = f"{float(getattr(det, 'score', 0.0)):.2f}"
            cells[3].text = str(getattr(det, "x1", 0))
            cells[4].text = str(getattr(det, "y1", 0))
            cells[5].text = f"{getattr(det, 'x2', 0)}/{getattr(det, 'y2', 0)}"

    document.add_paragraph()
    note = document.add_paragraph()
    note_run = note.add_run(
        "说明：防治建议为通用植保常识，具体用药请遵循当地植保部门指导与农药标签。"
    )
    note_run.font.size = Pt(9)

    target = task_dir(settings, task.id) / f"{_report_basename(task)}.docx"
    document.save(str(target))
    return target


# ---------------------------------------------------------------- html
def build_html(
    task: Task,
    settings: Any,
    counts: dict[str, int],
    detections: Sequence[Any],
    species: dict[str, dict[str, Any]],
    annotated_path: Path | None,
) -> Path:
    escape = html.escape
    total = sum(counts.values()) or 1

    meta_rows = "".join(
        f"<tr><th>{escape(label)}</th><td>{escape(value)}</td></tr>"
        for label, value in _task_meta_rows(task, counts)
    )
    count_rows = "".join(
        f"<tr><td>{escape(name)}</td><td class='num'>{count}</td><td class='num'>{count / total * 100:.1f}%</td></tr>"
        for name, count in sorted(counts.items(), key=lambda item: -item[1])
    )
    knowledge = "".join(
        "<section class='card'><h3>{zh}</h3>"
        "<p><b>寄主作物：</b>{host}</p>"
        "<p><b>危害特征：</b>{damage}</p>"
        "<p><b>防治建议：</b>{control}</p>"
        "<p><b>防治参考阈值：</b>{threshold}</p></section>".format(
            zh=escape(name),
            host=escape(item.get("host_crops") or "-"),
            damage=escape(item.get("damage") or "-"),
            control=escape(item.get("control") or "-"),
            threshold=escape(item.get("threshold_desc") or "-"),
        )
        for name, item in species.items()
    )
    detail_rows = "".join(
        "<tr><td>{i}</td><td>{frame}</td><td>{cls}</td><td>{zh}</td>"
        "<td class='num'>{score:.2f}</td><td class='num'>{x1}</td><td class='num'>{y1}</td>"
        "<td class='num'>{x2}</td><td class='num'>{y2}</td><td class='num'>{track}</td></tr>".format(
            i=index + 1,
            frame=getattr(det, "frame_index", 1),
            cls=escape(str(getattr(det, "class_name", ""))),
            zh=escape(str(getattr(det, "zh_name", "") or "")),
            score=float(getattr(det, "score", 0.0)),
            x1=getattr(det, "x1", 0),
            y1=getattr(det, "y1", 0),
            x2=getattr(det, "x2", 0),
            y2=getattr(det, "y2", 0),
            track=getattr(det, "track_id", None),
        )
        for index, det in enumerate(detections[:MAX_DETAIL_ROWS])
    )

    image_block = ""
    if annotated_path is not None and annotated_path.exists():
        image_block = f"<h2>标注图</h2><img class='shot' src='{annotated_path.name}' alt='标注图'>"

    document = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>病虫害检测报告 #{task.id}</title>
<style>
 body{{font-family:"Microsoft YaHei",system-ui,sans-serif;background:#f6f8f5;color:#22301c;margin:0;padding:32px}}
 h1{{margin:0 0 4px}} h2{{margin-top:32px;border-left:4px solid #2f9e5f;padding-left:10px}}
 .sub{{color:#7a8b72;margin-bottom:24px}}
 table{{border-collapse:collapse;width:100%;background:#fff;border-radius:8px;overflow:hidden}}
 th,td{{border-bottom:1px solid #e6ece3;padding:8px 12px;text-align:left;font-size:14px}}
 th{{background:#e8f2e6;width:180px}} td.num{{text-align:right}}
 .card{{background:#fff;border-radius:8px;padding:12px 16px;margin:10px 0;border:1px solid #e6ece3}}
 .card h3{{margin:0 0 8px;color:#2f9e5f}} .card p{{margin:4px 0;font-size:14px;line-height:1.6}}
 .shot{{max-width:100%;border-radius:8px;border:1px solid #e6ece3}}
 .foot{{margin-top:28px;color:#8b9a83;font-size:12px}}
</style></head><body>
<h1>病虫害检测报告</h1>
<div class="sub">任务 #{task.id} · 生成于 {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>
<table>{meta_rows}</table>
<h2>分类计数</h2>
<table><tr><th>虫种</th><th>数量</th><th>占比</th></tr>{count_rows}</table>
{image_block}
<h2>植保知识</h2>
{knowledge or "<p>未命中已知虫种。</p>"}
<h2>检测明细（前 {min(len(detections), MAX_DETAIL_ROWS)} 条）</h2>
<table><tr><th>#</th><th>帧号</th><th>类别</th><th>中文名</th><th>置信度</th>
<th>x1</th><th>y1</th><th>x2</th><th>y2</th><th>轨迹ID</th></tr>{detail_rows}</table>
<div class="foot">防治建议为通用植保常识，具体用药请遵循当地植保部门指导与农药标签说明。</div>
</body></html>"""

    target = task_dir(settings, task.id) / f"{_report_basename(task)}.html"
    target.write_text(document, encoding="utf-8")
    return target


# ---------------------------------------------------------------- 统一入口
def build_report(
    task: Task,
    *,
    settings: Any,
    counts: dict[str, int],
    detections: Sequence[Any],
    species: dict[str, dict[str, Any]],
    fmt: str,
) -> Path:
    """settings 显式传入，不用模块级全局持有 —— 否则多实例（例如测试）会互相串台。"""
    annotated = None
    candidate = settings.abs_outputs_dir / f"{task.id:06d}" / "annotated.jpg"
    if candidate.exists():
        annotated = candidate
    # 视频不嵌入报告，避免体积爆炸

    fmt = (fmt or "xlsx").lower()
    if fmt == "xlsx":
        return build_xlsx(task, settings, counts, detections, species, annotated)
    if fmt == "docx":
        return build_docx(task, settings, counts, detections, species, annotated)
    if fmt == "html":
        return build_html(task, settings, counts, detections, species, annotated)
    raise ValueError(f"不支持的报告格式：{fmt}")
