# -*- coding: utf-8 -*-
"""模型注册表测试。

包含一条对项目实际问题的回归断言：
**精度最高的 yolo11s 权重必须能被发现且指标正确**（旧代码里它被完全闲置）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import load_settings
from app.models import ModelInfo
from app.services.registry import (
    best_recommendation,
    discover,
    guess_arch,
    ranked,
    read_metrics,
    sync,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
YOLO11S_RUN = PROJECT_ROOT / "runs" / "detect" / "train_yolo11s_InsectPest"
BEST_WEIGHTS = PROJECT_ROOT / "weights" / "best-yolo11s.pt"

#: 只有需要"磁盘上真有权重"的用例才加这个标记：
#: 权重被 .gitignore 排除（二进制不进仓库），CI 上没有它。
#: 纯逻辑用例（架构名解析、results.csv 指标解析）在 CI 上照常跑。
needs_weights = pytest.mark.skipif(
    not BEST_WEIGHTS.is_file(), reason=f"缺少权重 {BEST_WEIGHTS.name}（仓库不存二进制）"
)

#: runs/ 是**训练产物**，部署包（给树莓派的那份）为了体积把它裁掉了。
#: 依赖真实训练产物的用例在这里跳过；解析逻辑本身由下面
#: `test_read_metrics_from_a_synthetic_results_csv` 兜底，不会因为裁剪而失去覆盖。
needs_runs = pytest.mark.skipif(
    not (YOLO11S_RUN / "results.csv").is_file(),
    reason="缺少 runs/ 训练产物目录（部署包为裁剪版）",
)


def test_guess_arch_from_various_filenames():
    assert guess_arch("best-yolov8n1") == "yolov8n"
    assert guess_arch("yolo11s") == "yolo11s"
    assert guess_arch("best-yolov5nu") == "yolov5n"
    # 训练目录名没有 yolo 前缀，也要能识别（否则模型列表里架构列是空的）
    assert guess_arch("train_v8_InsectPest3") == "yolov8"
    assert guess_arch("train_v5_InsectPest3") == "yolov5"
    assert guess_arch("train_yolo11s_InsectPest") == "yolo11s"
    assert guess_arch("something-else") is None


@needs_runs
def test_read_metrics_from_real_training_run():
    """直接用项目里真实的 results.csv 验证解析结果。"""
    metrics = read_metrics(YOLO11S_RUN / "results.csv")

    assert metrics["map50_95"] is not None
    assert metrics["map50"] is not None
    # 实测最优：mAP50 0.938 / mAP50-95 0.762
    assert 0.70 < metrics["map50_95"] < 0.80
    assert 0.90 < metrics["map50"] < 0.96
    assert metrics["epochs"] and metrics["epochs"] > 100


def test_read_metrics_from_a_synthetic_results_csv(tmp_path):
    """解析逻辑的**自包含**验证：不依赖仓库里的 runs/ 训练产物。

    runs/ 是几百 MB 的训练产出，部署包会裁掉它；如果只靠真实产物做断言，
    裁剪后的包里这条解析逻辑就完全没有测试了。这里手工造一份 results.csv，
    把"取历史最优而不是最后一轮""列名带空格要 strip""残缺行要跳过"三件事钉死。
    """
    csv_path = tmp_path / "results.csv"
    csv_path.write_text(
        "epoch,       metrics/mAP50(B),         metrics/mAP50-95(B)\n"
        "0,           0.10,                     0.05\n"
        "1,           0.93,                     0.76\n"
        "2,           0.80,                     0.60\n"   # 最后一轮不是最优
        "3,           n/a,                      \n"       # 残缺行：不应让解析崩掉
        "",
        encoding="utf-8",
    )

    metrics = read_metrics(csv_path)

    # 取的是历史最优 0.93/0.76，而不是最后一轮可解析的 0.80/0.60
    assert metrics["map50"] == pytest.approx(0.93)
    assert metrics["map50_95"] == pytest.approx(0.76)
    assert metrics["epochs"] == 3


def test_read_metrics_missing_file_is_empty():
    assert read_metrics(PROJECT_ROOT / "runs" / "does-not-exist" / "results.csv") == {}


@needs_weights
def test_discover_finds_the_canonical_models():
    """项目现在只保留效果最好的模型：`weights/best-yolo11s.pt` 及其边缘 ONNX。

    （原先这里有 5 个散落的 .pt + 3 份训练产出；旧的已按"只保留最好的模型"清理，
    但每次训练的 args.yaml / results.csv 仍保留在 runs/ 里作为记录。）
    """
    settings = load_settings()
    models = discover(settings)

    paths = {m["path"] for m in models}
    assert "weights/best-yolo11s.pt" in paths
    assert "weights/edge/best-yolo11s-640.onnx" in paths

    # 断言**意图**而不是位置：最好的模型必须可被发现，并且**带着指标** ——
    # 指标来自同名的 .metrics.json 旁挂文件；丢了它模型页会显示"—"，
    # 而且 best_recommendation 会反过来推荐一个精度更差的模型。
    yolo11s = [m for m in models if m["arch"] == "yolo11s" and m["source"] == "weights"]
    assert yolo11s, "未发现 yolo11s 正式权重"
    assert yolo11s[0]["path"] == "weights/best-yolo11s.pt"
    assert yolo11s[0]["map50_95"] is not None, "最好模型的指标丢了（检查 .metrics.json 旁挂文件）"
    assert yolo11s[0]["map50_95"] > 0.75


def test_sidecar_metrics_give_loose_weights_their_metrics(tmp_path):
    """脱离 runs 目录的权重，靠同名 `<stem>.metrics.json` 带上指标。

    这是"把最好的模型挪到 weights/ 当正式模型"之后指标不再丢失的保证。
    """
    from dataclasses import replace

    weights = tmp_path / "weights"
    weights.mkdir()
    (weights / "best-thing.pt").write_bytes(b"not-a-real-model")
    (weights / "best-thing.metrics.json").write_text(
        json.dumps({"map50": 0.91, "map50_95": 0.7, "epochs": 100}), encoding="utf-8"
    )
    (weights / "no-metrics.pt").write_bytes(b"not-a-real-model")

    isolated = replace(load_settings(), project_root=tmp_path)
    by_path = {m["path"]: m for m in discover(isolated)}

    with_metrics = by_path["weights/best-thing.pt"]
    assert with_metrics["map50_95"] == 0.7
    assert with_metrics["map50"] == 0.91
    assert with_metrics["epochs"] == 100

    without = by_path["weights/no-metrics.pt"]
    assert without["map50_95"] is None, "没有旁挂文件时应如实为 None，而不是编造指标"


@needs_weights
def test_sync_populates_db_and_flags_exactly_one_default(app):
    settings = app.state.settings
    db = app.state.session_factory()
    try:
        sync(db, settings)
        rows = db.query(ModelInfo).all()
        # 只保留最好模型后，**磁盘上仍在**的条目只有它和它的边缘 ONNX。
        # 库里可能还留着历史行（exists=False），但不该出现在模型页上。
        available = [r for r in rows if r.exists]
        assert len(available) >= 2
        assert all(r.path.startswith("weights/") for r in available)
        assert sum(1 for r in rows if r.is_default) == 1

        # 关键回归：精度最高的模型必须被识别为推荐项，
        # 证明「更好的权重被闲置」这个问题在数据层已可被发现
        recommendation = best_recommendation(rows)
        assert recommendation is not None
        assert recommendation.arch == "yolo11s"
        assert recommendation.map50_95 is not None and recommendation.map50_95 > 0.75
    finally:
        db.close()


def test_sync_marks_vanished_files_as_missing(app, tmp_path):
    """磁盘上已删除的权重不应继续被当成可用模型。

    用 tmp_path 自建权重目录，而不是依赖仓库里真实的 weights/：
    部署包裁掉了权重，那时库里一个模型都没有，`best_recommendation()` 返回 None，
    用例会以 `AttributeError` 失败 —— 红得莫名其妙，掩盖了它真正要守的行为。
    """
    from dataclasses import replace

    weights = tmp_path / "weights"
    weights.mkdir()
    (weights / "real.pt").write_bytes(b"not-a-real-model")
    # best_recommendation 只考虑"有指标"的模型，所以给它一份旁挂指标，
    # 这样"幽灵模型指标更好（0.99）但文件已不在"才构成真正的考验。
    (weights / "real.metrics.json").write_text(
        json.dumps({"map50": 0.90, "map50_95": 0.70, "epochs": 100}), encoding="utf-8"
    )

    isolated = replace(app.state.settings, project_root=tmp_path)
    db = app.state.session_factory()
    try:
        sync(db, isolated)
        ghost = ModelInfo(name="ghost", path="weights/ghost.pt", map50_95=0.99)
        db.add(ghost)
        db.commit()

        sync(db, isolated)          # 再同步一次

        db.refresh(ghost)
        assert ghost.exists is False
        assert ghost.is_default is False
        # 幽灵模型不应被推荐：推荐的是磁盘上真实存在的那个
        recommendation = best_recommendation(db.query(ModelInfo).all())
        assert recommendation is not None, "磁盘上明明有一个真实权重，却推荐不出来"
        assert recommendation.path == "weights/real.pt"
    finally:
        db.close()


def test_sync_survives_a_pt_and_its_exported_onnx(app, tmp_path):
    """同一份权重的 `.pt` 和导出给树莓派的 `.onnx` 必须能同时进库。

    这是**唯一正常的部署路径**（README：训练产出 `weights/best.pt`，
    再导出 `weights/best.onnx` 给边缘），而两条扫描分支都拿 `file.stem`
    当显示名 —— 于是两个不同的 path 抢同一个 `name`，撞上 `ModelInfo.name`
    的 unique 约束，`sync` 抛 `IntegrityError`，模型页整个 500。

    这里断言的是"能同时存在"，不是某个具体的显示名 ——
    名字怎么去重是实现细节，**不能同时存在**才是 bug。
    """
    from dataclasses import replace

    weights = tmp_path / "weights"
    weights.mkdir()
    (weights / "best.pt").write_bytes(b"not-a-real-model")
    (weights / "best.onnx").write_bytes(b"not-a-real-model")

    isolated = replace(app.state.settings, project_root=tmp_path)
    db = app.state.session_factory()
    try:
        rows = sync(db, isolated)
        by_path = {row.path: row for row in rows}
        assert set(by_path) == {"weights/best.pt", "weights/best.onnx"}
        assert len({row.name for row in rows}) == 2, "两个不同的权重不能共用一个显示名"
    finally:
        db.close()


def test_ranked_puts_missing_metrics_last():
    rows = [
        ModelInfo(name="a", path="a.pt", map50_95=None),
        ModelInfo(name="b", path="b.pt", map50_95=0.5),
        ModelInfo(name="c", path="c.pt", map50_95=0.76),
    ]
    ordered = ranked(rows)
    assert [m.name for m in ordered] == ["c", "b", "a"]
