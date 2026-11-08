# -*- coding: utf-8 -*-
"""把训练好的权重导出为边缘部署格式（ONNX / NCNN / TensorRT / TorchScript）。

【为什么建议用独立的导出环境】
导出工具链（onnx / onnxsim / rknn-toolkit2 / hailo_model_zoo）依赖重且互相冲突，
而主环境（torch 2.5.1+cu124 + ultralytics 8.3.203）是**已验证可用**的组合：
它被钉在 numpy 1.24.3，一旦为了装 onnxruntime 而升到 numpy 2.x，
桌面端、训练脚本、Web 后端都可能一起出问题。

所以本脚本的策略是：
  - 当前环境已有 onnx 就直接导出；
  - 否则**打印一条创建独立导出环境的命令**，而不是强行安装。
这比"为了跑一次导出把主环境搞坏"要划算得多。

用法：
    # 看有哪些候选模型（不需要任何额外依赖）
    D:\\python\\envs\\yolov5\\python.exe run_export_model.py --list

    # 导出精度最高的模型为 FP16 ONNX，并做速度对比
    D:\\python\\envs\\yolov5\\python.exe run_export_model.py --best --format onnx --half --benchmark

    # 导出给 ARM 板子的 NCNN（ultralytics 对 ARM 的推荐格式）
    D:\\python\\envs\\yolov5\\python.exe run_export_model.py --best --format ncnn

    # INT8 量化（需要校准集）
    D:\\python\\envs\\yolov5\\python.exe run_export_model.py --best --format onnx --int8 ^
        --data datasets/InsectPest/InsectPest.yaml

产出统一放在 weights/edge/ 下。
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

# 允许直接以脚本方式复用后端的模型注册表，避免两处各写一份扫描逻辑
from app.config import load_settings  # noqa: E402
from app.services.registry import best_recommendation, discover, guess_arch  # noqa: E402

EDGE_DIR = PROJECT_ROOT / "weights" / "edge"
DEFAULT_IMAGE = PROJECT_ROOT / "test_media" / "InsectPest_SIXU_A00025.jpg"

_FORMAT_DEPS = {
    "onnx": ("onnx", "onnx>=1.16"),
    "ncnn": ("ncnn", "ncnn"),
    "engine": ("tensorrt", "tensorrt"),
    "torchscript": (None, None),
}


def _has(module: str | None) -> bool:
    if module is None:
        return True
    return importlib.util.find_spec(module) is not None


def _export_venv_hint(missing: str, pip_name: str) -> None:
    print("")
    print("!" * 68)
    print(f"当前环境缺少 `{missing}`，无法导出该格式。")
    print("")
    print("建议**不要**直接装进主环境（会牵动 numpy/torch 的已验证组合），")
    print("而是建一个独立的导出环境：")
    print("")
    print("    conda create -n insect-export python=3.10 -y")
    print("    conda activate insect-export")
    print('    pip install ultralytics onnx onnxruntime onnxsim "onnx>=1.16"')
    print(f'    pip install "{pip_name}"')
    print("    # 然后在该环境里重新执行刚才那条导出命令（--list 不需要额外依赖）")
    print("!" * 68)
    print("注意：版本约束要加引号，否则 shell 会把 > 当成重定向。")


def cmd_list() -> int:
    settings = load_settings()
    models = discover(settings)
    if not models:
        print("未发现任何权重。")
        return 1

    print(f"{'模型':<40} {'架构':<10} {'mAP50':>7} {'mAP50-95':>9}  路径")
    print("-" * 100)
    for item in sorted(models, key=lambda m: (m.get("map50_95") is None, -(m.get("map50_95") or 0))):
        print(
            "{:<40} {:<10} {:>7} {:>9}  {}".format(
                item["name"][:40],
                item.get("arch") or "-",
                f"{item['map50']:.4f}" if item.get("map50") is not None else "-",
                f"{item['map50_95']:.4f}" if item.get("map50_95") is not None else "-",
                item["path"],
            )
        )

    class _Row:
        def __init__(self, data: dict) -> None:
            self.__dict__.update(data)
            self.map50_95 = data.get("map50_95")
            self.exists = True

    best = best_recommendation([_Row(m) for m in models])  # type: ignore[arg-type]
    if best is not None:
        print("-" * 100)
        print(f"推荐导出（精度最高）：{best.name}  mAP50-95={best.map50_95:.4f}  ->  {best.path}")
    return 0


def _resolve_model(args) -> Path:
    settings = load_settings()
    if args.model:
        path = Path(args.model)
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    models = discover(settings)
    if not models:
        raise SystemExit("未发现任何权重，请用 --model 显式指定。")

    if args.best:
        candidates = [m for m in models if m.get("map50_95") is not None]
        if not candidates:
            raise SystemExit("没有带指标的模型，无法判断最优；请用 --model 指定。")
        chosen = max(candidates, key=lambda m: m["map50_95"])
    else:
        # 默认用配置里的默认模型
        default_rel = settings.inference.default_model.replace("\\", "/")
        chosen = next((m for m in models if m["path"] == default_rel), models[0])

    print(f"选用模型：{chosen['name']}（{chosen['path']}）")
    return (PROJECT_ROOT / chosen["path"]).resolve()


def _benchmark_pytorch(weights: Path, imgsz: int, runs: int) -> float | None:
    try:
        import cv2
        import numpy as np
        from ultralytics import YOLO
    except ImportError as exc:
        print(f"  跳过 PyTorch 基准测试：{exc}")
        return None

    if not DEFAULT_IMAGE.exists():
        print(f"  跳过基准测试：缺少测试图片 {DEFAULT_IMAGE}")
        return None

    buffer = np.fromfile(str(DEFAULT_IMAGE), dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)

    model = YOLO(str(weights))
    model.predict(image, imgsz=imgsz, verbose=False)   # 预热
    started = time.perf_counter()
    for _ in range(runs):
        model.predict(image, imgsz=imgsz, verbose=False)
    elapsed = (time.perf_counter() - started) / runs * 1000
    print(f"  PyTorch   平均 {elapsed:7.1f} ms/帧  ({1000 / elapsed:.1f} FPS)")
    return elapsed


def _benchmark_onnx(onnx_path: Path, imgsz: int, runs: int) -> float | None:
    if not _has("onnxruntime"):
        print("  跳过 ONNX 基准测试：未安装 onnxruntime")
        return None
    try:
        import cv2
        import numpy as np
        import onnxruntime as ort
    except ImportError as exc:
        print(f"  跳过 ONNX 基准测试：{exc}")
        return None

    buffer = np.fromfile(str(DEFAULT_IMAGE), dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    image = cv2.resize(image, (imgsz, imgsz))
    tensor = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    tensor = np.transpose(tensor, (2, 0, 1))[None, ...]

    session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name
    session.run(None, {input_name: tensor})   # 预热
    started = time.perf_counter()
    for _ in range(runs):
        session.run(None, {input_name: tensor})
    elapsed = (time.perf_counter() - started) / runs * 1000
    print(f"  ONNX(CPU) 平均 {elapsed:7.1f} ms/帧  ({1000 / elapsed:.1f} FPS)")
    return elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description="导出边缘部署格式")
    parser.add_argument("--list", action="store_true", help="只列出候选模型")
    parser.add_argument("--model", help="权重路径（相对项目根或绝对路径）")
    parser.add_argument("--best", action="store_true", help="自动选择 mAP50-95 最高的模型")
    parser.add_argument(
        "--format",
        default="onnx",
        choices=["onnx", "ncnn", "engine", "torchscript"],
        help="导出格式（engine 需要 TensorRT）",
    )
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--half", action="store_true", help="FP16（GPU 导出）")
    parser.add_argument("--int8", action="store_true", help="INT8 量化（需 --data 校准集）")
    parser.add_argument("--data", help="校准集 yaml（INT8 必填）")
    parser.add_argument("--benchmark", action="store_true", help="对比 PyTorch 与 ONNX 的速度")
    parser.add_argument("--runs", type=int, default=20, help="基准测试次数")
    parser.add_argument("--simplify", action="store_true", default=True, help="ONNX 图简化")
    args = parser.parse_args()

    if args.list:
        return cmd_list()

    weights = _resolve_model(args)
    if not weights.exists():
        print(f"[错误] 权重不存在：{weights}")
        return 1

    if args.int8 and not args.data:
        print("[错误] --int8 需要同时提供 --data 校准集（否则量化精度会不可控）")
        return 1

    module, pip_name = _FORMAT_DEPS[args.format]
    if not _has(module):
        _export_venv_hint(module or "", pip_name or "")
        return 2

    try:
        from ultralytics import YOLO
    except ImportError:
        print("[错误] 未安装 ultralytics，请在该环境里先安装。")
        return 1

    print("")
    print(f"导出：{weights.name} -> {args.format}  imgsz={args.imgsz} "
          f"half={args.half} int8={args.int8} simplify={args.simplify}")

    EDGE_DIR.mkdir(parents=True, exist_ok=True)
    model = YOLO(str(weights))
    export_kwargs = {
        "format": args.format,
        "imgsz": args.imgsz,
        "half": bool(args.half),
        "int8": bool(args.int8),
        "simplify": bool(args.simplify),
    }
    if args.data:
        export_kwargs["data"] = str((PROJECT_ROOT / args.data).resolve())

    try:
        produced = model.export(**export_kwargs)
    except Exception as exc:  # noqa: BLE001 - 把失败原因原样告诉使用者
        print(f"[错误] 导出失败：{type(exc).__name__}: {exc}")
        return 1

    produced_path = Path(str(produced))
    if not produced_path.is_absolute():
        produced_path = (PROJECT_ROOT / produced_path).resolve()

    # 统一挪到 weights/edge/，方便与主权重区分
    suffix = produced_path.suffix if produced_path.is_file() else ""
    tag = f"{weights.stem}-{args.imgsz}" + ("-fp16" if args.half else "") + ("-int8" if args.int8 else "")
    final = EDGE_DIR / f"{tag}{suffix or ''}"
    try:
        if produced_path.is_file():
            import shutil

            shutil.move(str(produced_path), str(final))
        else:
            # ncnn 之类是目录
            import shutil

            if final.exists():
                shutil.rmtree(final, ignore_errors=True)
            shutil.move(str(produced_path), str(final))
    except OSError as exc:
        print(f"[警告] 产物已生成但移动失败（{exc}），原位置：{produced_path}")
        final = produced_path

    if final.is_file():
        print(f"完成：{final}  ({final.stat().st_size / 1024 / 1024:.1f} MB)")
    else:
        total = sum(f.stat().st_size for f in final.rglob("*") if f.is_file())
        print(f"完成：{final}/  (合计 {total / 1024 / 1024:.1f} MB)")

    if args.benchmark:
        print("")
        print("速度对比（同一张测试图）：")
        torch_ms = _benchmark_pytorch(weights, args.imgsz, args.runs)
        if args.format == "onnx" and final.is_file():
            onnx_ms = _benchmark_onnx(final, args.imgsz, args.runs)
            if torch_ms and onnx_ms:
                print(f"  -> ONNX 相对 PyTorch 加速 {torch_ms / onnx_ms:.2f}x")
        print("说明：这里测的是**本机 CPU/GPU**，边缘板子的实际帧率必须在目标设备上实测。")

    print("")
    print("下一步：把产物放到边缘设备，并修改 config.yaml：")
    print("    inference:")
    print(f"      backend: {args.format if args.format in ('onnx',) else 'onnx'}")
    print(f"      default_model: {final.relative_to(PROJECT_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
