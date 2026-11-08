# -*- coding: utf-8 -*-
"""知识库参考图（取自数据集的样本照片）的测试。

这些用例需要本机存在训练数据集（`datasets/InsectPest/*/labels` + `images`）。
树莓派上通常只部署模型、不带数据集，因此整体用 `skipif` 保护：
缺数据集时**跳过**，而不是失败 —— 与接口本身"取不到图就退回占位"的降级策略一致。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services import species_samples

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET_ROOT = PROJECT_ROOT / "datasets" / "InsectPest"

# 注意必须同时检查 images：本仓库刻意不跟踪数据集图像（体积大），
# 只跟踪 labels —— 于是 CI 上 labels 存在而 images 缺失，
# 只查 labels 会把用例放进"假可跑"状态然后红掉（真实发生过）
HAS_DATASET = DATASET_ROOT.is_dir() and any(
    (DATASET_ROOT / split / "labels").is_dir()
    and (DATASET_ROOT / split / "images").is_dir()
    for split in ("train", "valid", "test")
)
needs_dataset = pytest.mark.skipif(not HAS_DATASET, reason="本机没有训练数据集，跳过参考图用例")

IMAGE_MAGIC = (b"\xff\xd8", b"\x89PNG")


@pytest.fixture()
def auth_headers(client: TestClient) -> dict[str, str]:
    creds = {"username": "sampler", "password": "sampler12345"}
    client.post("/api/auth/register", json=creds)
    token = client.post("/api/auth/login", json=creds).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- 扫描逻辑
@needs_dataset
def test_every_class_has_a_sample_image(settings):
    """数据集的 15 个类别都应能挑出至少一张图（这是配图功能的前提）。"""
    samples = species_samples.samples_for(settings)
    assert len(samples) >= 15, f"只找到 {len(samples)} 个类别的样本图"
    for class_id, path in samples.items():
        assert path.is_file(), f"class {class_id} 的样本图不存在: {path}"


@needs_dataset
def test_samples_are_cached_into_data_dir(settings, tmp_path):
    """第一次扫描应把样本图复制进 data_dir —— 树莓派上没有数据集时靠这份缓存。"""
    species_samples.reset_cache()
    samples = species_samples.samples_for(settings)
    assert samples, "应扫到样本图"

    cache_dir = settings.data_dir / "species_samples"
    assert cache_dir.is_dir(), "未建立缓存目录"
    cached_files = list(cache_dir.glob("*"))
    assert len(cached_files) >= 15, f"只缓存了 {len(cached_files)} 张"
    # 返回的路径应指向缓存副本，而不是数据集原图
    for path in samples.values():
        assert path.parent == cache_dir, f"仍在引用数据集原图: {path}"


def _make_fake_dataset(root: Path, labels: dict[str, str], images: dict[str, bytes]) -> None:
    """造一个最小的 YOLO 数据集（只用来验证缓存逻辑，不动真实数据集）。"""
    label_dir = root / "datasets" / "InsectPest" / "train" / "labels"
    image_dir = root / "datasets" / "InsectPest" / "train" / "images"
    label_dir.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)
    for name, text in labels.items():
        (label_dir / f"{name}.txt").write_text(text, encoding="utf-8")
    for name, payload in images.items():
        (image_dir / f"{name}.jpg").write_bytes(payload)


def test_cache_refreshes_when_dataset_changes(settings, tmp_path):
    """数据集变了，缓存必须跟着变 —— 否则重新标注后配图会静默停在旧版本。"""
    from dataclasses import replace

    project = tmp_path / "proj"
    _make_fake_dataset(
        project,
        labels={"a": "0 0.5 0.5 0.2 0.2\n"},
        images={"a": b"\xff\xd8old"},
    )
    isolated = replace(settings, project_root=project, data_dir=tmp_path / "data")

    species_samples.reset_cache()
    first = species_samples.samples_for(isolated)
    assert set(first) == {0}
    assert first[0].read_bytes() == b"\xff\xd8old"

    # 类别从 0 改成 7、图片内容换掉，并新增类别 3
    _make_fake_dataset(
        project,
        labels={"a": "7 0.5 0.5 0.2 0.2\n", "b": "3 0.5 0.5 0.2 0.2\n"},
        images={"a": b"\xff\xd8new", "b": b"\xff\xd8bee"},
    )

    second = species_samples.refresh_cache(isolated)
    assert set(second) == {3, 7}, "缓存没跟着数据集更新"
    assert second[7].read_bytes() == b"\xff\xd8new", "缓存里还是旧图"
    assert not (second[7].parent / "0.jpg").exists(), "旧类别的孤儿缓存没清掉"


def test_without_dataset_and_without_cache_returns_empty(tmp_path):
    """数据集与缓存都没有时应返回空映射（接口给 404、前端退回占位），而不是抛异常。"""
    from dataclasses import replace

    from app.config import load_settings

    base = load_settings()
    isolated = replace(base, project_root=tmp_path, data_dir=tmp_path / "data")
    species_samples.reset_cache()
    assert species_samples.samples_for(isolated) == {}
    assert species_samples.sample_for(isolated, 0) is None


# ---------------------------------------------------------------- 接口
def test_sample_endpoint_is_anonymous_by_design(client: TestClient):
    """卡片里是 `<img src=...>`，浏览器不会附带 Authorization 头。

    所以这个接口**不能**要求登录（与标注图挂 `/media/...` 的策略一致）。
    这里把"不带鉴权也不会 401"这个刻意的决定钉住，防止以后有人顺手加上依赖。
    """
    resp = client.get("/api/species/sample/0")
    assert resp.status_code in (200, 404), resp.text
    assert resp.status_code not in (401, 403), "参考图接口被加上鉴权了？<img> 打不开它"


@needs_dataset
def test_sample_endpoint_returns_real_image_bytes(client: TestClient):
    resp = client.get("/api/species/sample/0")
    assert resp.status_code == 200
    assert resp.content[:2] in IMAGE_MAGIC or resp.content[:4] in IMAGE_MAGIC, "返回的不是图片"
    assert "max-age" in resp.headers.get("cache-control", "")


def test_unknown_class_returns_404(client: TestClient):
    assert client.get("/api/species/sample/9999").status_code == 404


@needs_dataset
def test_species_list_points_image_path_at_sample_endpoint(client: TestClient, auth_headers):
    rows = client.get("/api/species", headers=auth_headers).json()
    assert rows, "知识库不应为空"
    with_image = [row for row in rows if row.get("image_path")]
    assert len(with_image) >= 15, f"只有 {len(with_image)} 个类别拿到了参考图"
    assert with_image[0]["image_path"].startswith("/api/species/sample/")
