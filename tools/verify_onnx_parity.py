# -*- coding: utf-8 -*-
"""跨环境验证 ONNX 后端与 torch 后端的一致性。

**为什么必须跨环境**：边缘设备上不装 torch，所以两边没法在一个进程里对比。
做法是两个环境各自产出结果，再比对：

    主环境（有 torch）:
        python tools/verify_onnx_parity.py run --backend torch --out ref.json

    边缘环境（只有 onnxruntime）:
        python tools/verify_onnx_parity.py run --backend onnx \
            --model weights/edge/best-yolo11s-640.onnx --out onnx.json

    任意一个环境:
        python tools/verify_onnx_parity.py compare --a ref.json --b onnx.json

只比"检出的框是否一致"，不比耗时（耗时随设备变化）。
判定标准：同一 class 且 IoU >= 0.9 视为同一个框，分数差 <= 0.05。

用例刻意包含非方形与放大/缩小场景 —— letterbox 的补边数学只有在
长宽比不一致时才会暴露问题（方形输入下它退化成普通 resize，看似永远正确）。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import load_settings                      # noqa: E402
from app.services.detector import DetectorService          # noqa: E402

TEST_MEDIA = PROJECT_ROOT / "test_media"
DEFAULT_MODEL_TORCH = PROJECT_ROOT / "weights" / "best-yolo11s.pt"
DEFAULT_MODEL_ONNX = PROJECT_ROOT / "weights" / "edge" / "best-yolo11s-640.onnx"

CONF = 0.25
IOU = 0.5
MAX_DET = 300
IOU_MATCH = 0.9
SCORE_TOL = 0.05


def _read_bgr(path: Path) -> np.ndarray | None:
    """中文路径安全读图：np.fromfile + imdecode。"""
    try:
        buffer = np.fromfile(str(path), dtype=np.uint8)
        return cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    except Exception:  # noqa: BLE001
        return None


def _cases() -> list[tuple[str, np.ndarray]]:
    """构造覆盖各种 letterbox 情形的用例。"""
    sources = sorted(TEST_MEDIA.glob("*.jp*g"))[:3]
    images = [img for img in (_read_bgr(p) for p in sources) if img is not None]
    if not images:
        raise SystemExit(f"未在 {TEST_MEDIA} 找到可用测试图")

    base = images[0]
    cases: list[tuple[str, np.ndarray]] = [("orig_%s" % sources[0].stem, base)]
    # 强制产生补边：宽扁 / 竖长 / 小图（放大）/ 大图（缩小）
    cases.append(("wide_800x300", cv2.resize(base, (800, 300), interpolation=cv2.INTER_LINEAR)))
    cases.append(("tall_300x800", cv2.resize(base, (300, 800), interpolation=cv2.INTER_LINEAR)))
    cases.append(("small_200x200", cv2.resize(base, (200, 200), interpolation=cv2.INTER_LINEAR)))
    cases.append(("big_1280x1280", cv2.resize(base, (1280, 1280), interpolation=cv2.INTER_LINEAR)))
    for image in images[1:]:
        cases.append(("extra_%dx%d" % (image.shape[1], image.shape[0]), image))
    return cases


def run(backend: str, model: str | None, out_path: Path, device: str | None = None) -> int:
    settings = load_settings()
    target = model or (DEFAULT_MODEL_ONNX if backend == "onnx" else DEFAULT_MODEL_TORCH)
    inference = replace(
        settings.inference,
        backend=backend,
        default_model=str(target),
        conf=CONF,
        iou=IOU,
        max_det=MAX_DET,
        preload=False,
        heatmap=False,
    )
    if device:
        # 用于把「ONNX 与 torch 的差异」和「CPU 与 GPU 的差异」分开：
        # 让 torch 也跑 CPU，若两者随即一致，说明差异来自设备精度而非实现。
        inference = replace(inference, device=device)
    service = DetectorService(replace(settings, inference=inference))
    service.load()

    info = service.info()
    print("后端信息:", json.dumps(info, ensure_ascii=False))
    if info.get("fallback_reason"):
        print("!! 发生了后端回退，本次结果不能作为 ONNX 基准:", info["fallback_reason"])
        return 2
    if info.get("name") != backend:
        print("!! 期望后端 %s，实际 %s" % (backend, info.get("name")))
        return 2

    results: dict[str, dict] = {"backend": backend, "model": str(target), "cases": {}}
    for name, image in _cases():
        # max_det 由 settings.inference.max_det 控制（已在上面 replace 进 settings）
        result = service.infer(image, CONF, IOU)
        height, width = image.shape[:2]
        results["cases"][name] = {
            "shape": [width, height],
            "inference_ms": round(result.inference_ms, 2),
            "detections": [
                {
                    "class_id": det.class_id,
                    "class_name": det.class_name,
                    "score": round(det.score, 6),
                    "bbox": list(det.bbox),
                }
                for det in result.detections
            ],
        }
        print("  %-22s %4dx%-4d 检出 %d" % (name, width, height, result.count))

    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写出:", out_path)
    return 0


def _iou(a: list[int], b: list[int]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def compare(a_path: Path, b_path: Path) -> int:
    left = json.loads(a_path.read_text(encoding="utf-8"))
    right = json.loads(b_path.read_text(encoding="utf-8"))
    print("A = %s (%s)" % (left["backend"], Path(left["model"]).name))
    print("B = %s (%s)" % (right["backend"], Path(right["model"]).name))

    failures: list[str] = []
    total_a = total_b = total_matched = 0

    for name in sorted(set(left["cases"]) | set(right["cases"])):
        case_a = left["cases"].get(name)
        case_b = right["cases"].get(name)
        if case_a is None or case_b is None:
            failures.append("%s: 只有一侧有结果" % name)
            continue
        dets_a = case_a["detections"]
        dets_b = case_b["detections"]
        total_a += len(dets_a)
        total_b += len(dets_b)

        used: set[int] = set()
        matched = 0
        worst_iou = 1.0
        worst_score = 0.0
        for det_a in dets_a:
            best_index, best_iou = -1, 0.0
            for index, det_b in enumerate(dets_b):
                if index in used or det_b["class_id"] != det_a["class_id"]:
                    continue
                value = _iou(det_a["bbox"], det_b["bbox"])
                if value > best_iou:
                    best_index, best_iou = index, value
            if best_index >= 0 and best_iou >= IOU_MATCH:
                used.add(best_index)
                matched += 1
                worst_iou = min(worst_iou, best_iou)
                worst_score = max(worst_score, abs(det_a["score"] - dets_b[best_index]["score"]))
        total_matched += matched

        status = "OK" if matched == len(dets_a) == len(dets_b) else "不一致"
        print(
            "  %-22s A=%d B=%d 匹配=%d  最差IoU=%.4f 最大分数差=%.4f  %s"
            % (name, len(dets_a), len(dets_b), matched, worst_iou, worst_score, status)
        )
        if matched != len(dets_a) or matched != len(dets_b):
            failures.append(
                "%s: A 检出 %d, B 检出 %d, 匹配 %d" % (name, len(dets_a), len(dets_b), matched)
            )
        if worst_score > SCORE_TOL:
            failures.append("%s: 分数差 %.4f 超过容差 %.2f" % (name, worst_score, SCORE_TOL))

    print()
    print("合计：A %d 个框, B %d 个框, 匹配 %d 个" % (total_a, total_b, total_matched))
    if failures:
        print("结果：不一致")
        for item in failures:
            print("  -", item)
        return 1
    print("结果：完全一致（每个框都按同类别、IoU>=%.2f 配上，分数差 <= %.2f）" % (IOU_MATCH, SCORE_TOL))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ONNX 与 torch 后端一致性验证")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="跑一个后端并写出结果 JSON")
    run_parser.add_argument("--backend", choices=["torch", "onnx"], required=True)
    run_parser.add_argument("--model", help="权重路径（默认按后端选）")
    run_parser.add_argument("--device", help="强制设备（cpu / cuda:0），用于分离设备精度差异")
    run_parser.add_argument("--out", required=True, type=Path)

    cmp_parser = sub.add_parser("compare", help="比对两份结果")
    cmp_parser.add_argument("--a", required=True, type=Path)
    cmp_parser.add_argument("--b", required=True, type=Path)

    args = parser.parse_args()
    if args.command == "run":
        return run(args.backend, args.model, args.out, args.device)
    return compare(args.a, args.b)


if __name__ == "__main__":
    raise SystemExit(main())
