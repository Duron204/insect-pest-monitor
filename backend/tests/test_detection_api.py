# -*- coding: utf-8 -*-
"""检测 / 历史 / 统计 / 报告 接口的端到端测试。

会真正加载模型并跑推理，因此比其余用例慢，但覆盖的是最关键的业务链路。
视频部分不依赖后台线程的时序（那样容易 flaky），而是**直接同步调用**
`process_video_task`，从而确定性地验证「逐帧处理 + 边写标注视频 + 任务收敛」。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from app.models import Task, TaskMode, TaskStatus
from app.services.video import process_video_task

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_IMAGE = PROJECT_ROOT / "test_media" / "InsectPest_SIXU_A00025.jpg"
DEFAULT_WEIGHTS = PROJECT_ROOT / "weights" / "best-yolo11s.pt"
#: 权重被 .gitignore 排除（18MB 二进制不进仓库），因此 CI 上没有它。
#: 这种情况**显式跳过**并在报告里写清原因，而不是让 CI 无故失败。
pytestmark = pytest.mark.skipif(
    not DEFAULT_WEIGHTS.is_file(), reason=f"缺少权重 {DEFAULT_WEIGHTS.name}（仓库不存二进制）"
)

@pytest.fixture()
def auth_client(app, client):
    """注册 + 登录，并把令牌放进默认请求头。"""
    client.post(
        "/api/auth/register", json={"username": "tester", "password": "secret123"}
    )
    token = client.post(
        "/api/auth/login", json={"username": "tester", "password": "secret123"}
    ).json()["access_token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


@pytest.fixture()
def loaded_app(app):
    """测试夹具默认关闭了预加载，这里按需加载真实权重。"""
    app.state.detector.load(str(DEFAULT_WEIGHTS))
    return app


def _post_image(client, path: Path = TEST_IMAGE):
    with path.open("rb") as handle:
        return client.post(
            "/api/detection/image",
            files={"file": (path.name, handle, "image/jpeg")},
        )


def _make_video(path: Path, frames: int = 12, size: tuple[int, int] = (320, 240), fps: int = 10) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    assert writer.isOpened(), "无法创建测试视频"
    for index in range(frames):
        frame = np.full((size[1], size[0], 3), 200, dtype=np.uint8)
        cv2.circle(frame, (40 + index * 8, 120), 18, (30, 30, 30), -1)
        writer.write(frame)
    writer.release()


# ---------------------------------------------------------------- 单图检测
def test_detect_image_end_to_end(loaded_app, auth_client):
    response = _post_image(auth_client)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["task_id"] > 0
    assert body["frame_total"] >= 1, "数据集内图片应当有检出"
    assert body["unique_objects"] == body["frame_total"]   # 单帧：去重数 == 框数
    assert body["inference_ms"] > 0
    assert body["width"] > 0 and body["height"] > 0
    assert body["image_url"].startswith("/media/outputs/")
    # 产物地址带媒体票据（见 app/media_guard.py）：路径照旧，多一个 `?mt=`
    assert body["annotated_url"].startswith(
        f"/media/outputs/{body['task_id']:06d}/annotated.jpg?mt="
    )
    assert sum(body["counts"].values()) == body["frame_total"]

    for det in body["detections"]:
        assert det["zh_name"], "中文类别名未补全"
        assert len(det["bbox"]) == 4
        x1, y1, x2, y2 = det["bbox"]
        assert 0 <= x1 < x2 <= body["width"]
        assert 0 <= y1 < y2 <= body["height"]


def test_detection_output_is_served(loaded_app, auth_client):
    body = _post_image(auth_client).json()

    for url in (body["image_url"], body["annotated_url"]):
        response = auth_client.get(url)
        assert response.status_code == 200, f"静态产物未挂载：{url}"
        assert response.headers["content-type"].startswith("image/")


def test_detection_requires_auth(app, client):
    with TEST_IMAGE.open("rb") as handle:
        response = client.post(
            "/api/detection/image", files={"file": (TEST_IMAGE.name, handle, "image/jpeg")}
        )
    assert response.status_code == 401


def test_rejects_non_image_upload(loaded_app, auth_client):
    response = auth_client.post(
        "/api/detection/image",
        files={"file": ("not_an_image.jpg", b"definitely not an image", "image/jpeg")},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------- 批量
def test_batch_detection_counts_aggregate(loaded_app, auth_client):
    handles = []
    files = []
    try:
        for _ in range(2):
            handle = TEST_IMAGE.open("rb")
            handles.append(handle)
            files.append(("files", (TEST_IMAGE.name, handle, "image/jpeg")))
        response = auth_client.post("/api/detection/batch", files=files)
    finally:
        for handle in handles:
            handle.close()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert body["succeeded"] == 2
    assert body["failed"] == 0
    assert body["total_objects"] == sum(item["frame_total"] for item in body["items"])
    assert sum(body["counts"].values()) == body["total_objects"]


# ---------------------------------------------------------------- 历史
def test_task_history_detail_and_delete(loaded_app, auth_client):
    task_id = _post_image(auth_client).json()["task_id"]

    listing = auth_client.get("/api/tasks").json()
    assert listing["total"] >= 1
    assert task_id in [item["id"] for item in listing["items"]]

    detail = auth_client.get(f"/api/tasks/{task_id}")
    assert detail.status_code == 200
    assert detail.json()["counts"], "详情应带上分类计数"
    assert detail.json()["progress"] == 100.0

    detections = auth_client.get(f"/api/tasks/{task_id}/detections").json()
    assert detections["total"] >= 1

    assert auth_client.delete(f"/api/tasks/{task_id}").status_code == 204
    assert auth_client.get(f"/api/tasks/{task_id}").status_code == 404


def test_unknown_task_returns_404(loaded_app, auth_client):
    assert auth_client.get("/api/tasks/999999").status_code == 404


# ---------------------------------------------------------------- 统计
def test_stats_summary_reflects_tasks(loaded_app, auth_client):
    _post_image(auth_client)

    response = auth_client.get("/api/stats/summary?range=all")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["total_tasks"] >= 1
    assert body["unique_objects"] >= 1
    assert len(body["by_hour"]) == 24
    assert body["by_class"], "应当有分类统计"
    assert sum(item["count"] for item in body["by_class"]) >= 1


# ---------------------------------------------------------------- 报告
@pytest.mark.parametrize("fmt", ["xlsx", "docx", "html"])
def test_report_export(loaded_app, auth_client, fmt):
    task_id = _post_image(auth_client).json()["task_id"]

    response = auth_client.get(f"/api/tasks/{task_id}/report?format={fmt}")
    assert response.status_code == 200, response.text
    assert len(response.content) > 0


# ---------------------------------------------------------------- 视频
def test_video_endpoint_accepts_and_creates_task(loaded_app, auth_client, tmp_path):
    video_path = tmp_path / "clip.mp4"
    _make_video(video_path)

    with video_path.open("rb") as handle:
        response = auth_client.post(
            "/api/detection/video",
            files={"file": ("clip.mp4", handle, "video/mp4")},
        )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["task_id"] > 0
    assert body["source_name"] == "clip.mp4"


def test_process_video_task_writes_annotated_video(loaded_app, tmp_path):
    """直接同步跑完整视频流程，确定性地验证产物与任务收敛。"""
    app = loaded_app
    settings = app.state.settings
    session_factory = app.state.session_factory

    db = session_factory()
    try:
        task = Task(
            mode=TaskMode.VIDEO,
            status=TaskStatus.PENDING,
            source_name="synthetic.mp4",
            conf=0.25,
            iou=0.5,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        task_id = task.id
    finally:
        db.close()

    video_path = tmp_path / "synthetic.mp4"
    _make_video(video_path, frames=12, size=(320, 240), fps=10)

    process_video_task(
        session_factory=session_factory,
        settings=settings,
        detector=app.state.detector,
        task_id=task_id,
        video_path=video_path,
        conf=0.25,
        iou=0.5,
    )

    db = session_factory()
    try:
        row = db.get(Task, task_id)
        assert row is not None
        assert row.status == TaskStatus.DONE, row.error
        assert row.frame_count == 12
        assert row.finished_at is not None
        assert row.result_video_path and row.result_video_path.endswith("annotated.mp4")
    finally:
        db.close()

    output = settings.abs_outputs_dir / f"{task_id:06d}" / "annotated.mp4"
    assert output.exists(), "标注视频未生成"
    assert output.stat().st_size > 0


# ---------------------------------------------------------------- 契约细节
def test_timestamps_are_explicitly_marked_utc(loaded_app, auth_client):
    """全库存的是无时区 UTC；交给前端时必须显式带 Z，
    否则前端只能靠"猜"，很容易整体偏 8 小时。"""
    task_id = _post_image(auth_client).json()["task_id"]
    detail = auth_client.get(f"/api/tasks/{task_id}").json()

    assert detail["started_at"].endswith("Z"), detail["started_at"]
    assert detail["finished_at"] is None or detail["finished_at"].endswith("Z")

    listing = auth_client.get("/api/tasks").json()
    assert listing["items"][0]["started_at"].endswith("Z")


def test_detections_expose_frame_index(loaded_app, auth_client):
    """历史明细要能显示"第几帧"，否则视频任务的抽样明细无法定位。"""
    task_id = _post_image(auth_client).json()["task_id"]
    detections = auth_client.get(f"/api/tasks/{task_id}/detections").json()

    assert detections["total"] >= 1
    first = detections["items"][0]
    assert "frame_index" in first
    assert first["frame_index"] == 1
