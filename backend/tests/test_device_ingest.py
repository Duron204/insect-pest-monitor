# -*- coding: utf-8 -*-
"""设备上报检测结果：落库、幂等、归属、载荷防线。

**不需要权重、不需要 torch** —— 中心端从不对上报跑推理，只解码 + 烧框。
所以这里用的框坐标是手写的假框，图片用 `test_media/` 里的真 JPEG 字节
（真图是为了让 `persist_frame_task` 的"存原图 + 烧框 + 存标注图"整条真跑）。
"""
from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Device, DeviceIngest, Task, TaskMode, utcnow
from app.services import device_ingest as ingest_module
from app.services.device_ingest import _claim_seq, prune_ledger

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_IMAGE = PROJECT_ROOT / "test_media" / "InsectPest_SIXU_A00025.jpg"

HARDWARE_ID = "pi-serial-ingest"


# ---------------------------------------------------------------- 夹具
# 限流清零（`_reset_device_rate_limiters`）与注册助手（`register_user`）
# 三个设备测试文件共用，见 conftest.py。
@pytest.fixture()
def image_b64() -> str:
    """一张真实 JPEG 的 base64 —— 用真字节，烧框才是真烧。"""
    return base64.b64encode(SAMPLE_IMAGE.read_bytes()).decode("ascii")


@pytest.fixture()
def owner(register_user) -> dict[str, str]:
    return register_user("owner")


def _bind(client: TestClient, owner: dict[str, str]) -> tuple[dict[str, str], int]:
    """配对 → 绑定 → 取令牌。返回 (设备令牌请求头, 设备 id)。"""
    started = client.post(
        "/api/devices/pair/start",
        json={"hardware_id": HARDWARE_ID, "name": "东头盒子"},
    ).json()
    device = client.post(
        "/api/devices/bind", json={"code": started["code"]}, headers=owner
    ).json()
    # 轮询必须带上 `pair/start` 发的那枚凭证 —— 只凭 hardware_id 中心一律回 unknown
    token = client.get(
        "/api/devices/pair/poll",
        params={"hardware_id": HARDWARE_ID},
        headers={"X-Pairing-Secret": started["pairing_secret"]},
    ).json()["device_token"]
    return {"X-Device-Token": token}, device["id"]


def _payload(image_b64: str, seq: int = 1, **overrides) -> dict:
    body = {
        "seq": seq,
        "conf": 0.25,
        "iou": 0.5,
        "backend": "torch",
        "source_name": "树莓派 CSI 摄像头",
        "inference_ms": 42.5,
        "detections": [
            {
                "class_id": 0,
                "class_name": "aphid",
                "zh_name": "蚜虫",
                "score": 0.91,
                "bbox": [10, 20, 110, 140],
            },
            {
                "class_id": 3,
                "class_name": "whitefly",
                "zh_name": "白粉虱",
                "score": 0.77,
                "bbox": [200, 60, 260, 120],
            },
        ],
        "image_base64": image_b64,
    }
    body.update(overrides)
    return body


def _post(client: TestClient, headers: dict[str, str], body: dict):
    return client.post("/api/devices/tasks", json=body, headers=headers)


def _device_tasks(app, device_id: int) -> list[Task]:
    session = app.state.session_factory()
    try:
        return list(
            session.scalars(select(Task).where(Task.device_id == device_id)).all()
        )
    finally:
        session.close()


# ---------------------------------------------------------------- 鉴权
def test_ingest_requires_a_device_token(client: TestClient, image_b64):
    assert _post(client, {}, _payload(image_b64)).status_code == 401
    assert _post(client, {"X-Device-Token": "ipd_nope"}, _payload(image_b64)).status_code == 401


def test_user_jwt_is_not_a_device_token(client: TestClient, owner, image_b64):
    """两种身份严格分开 —— 用户令牌塞进设备头里必须不被认。

    这条守的是 `deps.py` 那个"独立头"的决定：一旦有人图省事把设备鉴权
    改成复用 Authorization，这里立刻会红。
    """
    bearer = owner["Authorization"].removeprefix("Bearer ")
    assert _post(client, {"X-Device-Token": bearer}, _payload(image_b64)).status_code == 401


def test_unbound_device_cannot_ingest(client: TestClient, owner, image_b64):
    headers, device_id = _bind(client, owner)
    assert _post(client, headers, _payload(image_b64)).status_code == 200

    client.post(f"/api/devices/{device_id}/unbind", headers=owner)
    response = _post(client, headers, _payload(image_b64, seq=2))
    assert response.status_code == 403
    assert "解绑" in response.json()["detail"]


# ---------------------------------------------------------------- 正常落库
def test_ingest_creates_a_complete_task(client: TestClient, app, owner, image_b64):
    headers, device_id = _bind(client, owner)
    response = _post(client, headers, _payload(image_b64))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["duplicate"] is False
    assert body["total_objects"] == 2
    assert body["counts"] == {"蚜虫": 1, "白粉虱": 1}

    task_id = body["task_id"]
    detail = client.get(f"/api/tasks/{task_id}", headers=owner).json()
    assert detail["mode"] == TaskMode.DEVICE
    assert detail["device_id"] == device_id
    assert detail["status"] == "done"
    assert detail["counts"] == {"蚜虫": 1, "白粉虱": 1}
    assert detail["result_image_path"], "中心应自己烧框并留下标注图"

    # 产物地址现在是**带票据**的；裸地址匿名取 401（这就是"账号分开"落到了磁盘上）。
    assert client.get(f"/media/outputs/{task_id:06d}/original.jpg").status_code == 401
    signed = detail["result_image_path"]
    ticket = signed.split("mt=", 1)[1]
    # 两张图都真的能取到（原图 + 服务端烧的标注图），票据按任务签发、与文件名无关
    for name in ("original.jpg", "annotated.jpg"):
        path = f"/media/outputs/{task_id:06d}/{name}?mt={ticket}"
        assert client.get(path).status_code == 200, path

    # 框明细落全了
    dets = client.get(f"/api/tasks/{task_id}/detections", headers=owner).json()
    assert dets["total"] == 2
    assert {d["class_name"] for d in dets["items"]} == {"aphid", "whitefly"}

    # 归属：任务算在绑定账号 + 这台设备 + 设备的地块名下
    session = app.state.session_factory()
    try:
        task = session.get(Task, task_id)
        assert task.user_id is not None
        assert task.device_id == device_id
    finally:
        session.close()


def test_ingest_touches_last_seen_and_last_task(client: TestClient, app, owner, image_b64):
    headers, device_id = _bind(client, owner)
    _post(client, headers, _payload(image_b64))

    listed = client.get("/api/devices", headers=owner).json()
    assert listed[0]["online"] is True, "刚上报过就该显示在线"
    assert listed[0]["last_task_at"] is not None


# ---------------------------------------------------------------- ⭐ 幂等
def test_resending_the_same_seq_does_not_duplicate(client: TestClient, app, owner, image_b64):
    """⭐ 断网重发绝不能变成两条历史。

    断言落在**数据库行数**上而不只是响应体 —— 只看响应体的话，
    "提前 return 但已经把行写进去了"这类 bug 是抓不到的，
    而那正是这个用例要防的东西（看板上的虫口数会凭空翻倍）。
    """
    headers, device_id = _bind(client, owner)

    first = _post(client, headers, _payload(image_b64, seq=7)).json()
    second = _post(client, headers, _payload(image_b64, seq=7)).json()

    assert second["task_id"] == first["task_id"]
    assert second["duplicate"] is True
    assert second["counts"] == first["counts"]

    # 关键：库里只有一条
    assert len(_device_tasks(app, device_id)) == 1
    session = app.state.session_factory()
    try:
        assert session.query(Task).filter(Task.mode == TaskMode.DEVICE).count() == 1
        assert session.query(DeviceIngest).count() == 1
        ledger = session.query(DeviceIngest).one()
        assert ledger.seq == 7
        assert ledger.task_id == first["task_id"]
    finally:
        session.close()


def test_different_seq_creates_a_second_task(client: TestClient, app, owner, image_b64):
    """幂等不能把"下一次抓拍"也吞掉 —— seq 变了就是新的一条。"""
    headers, device_id = _bind(client, owner)
    a = _post(client, headers, _payload(image_b64, seq=1)).json()
    b = _post(client, headers, _payload(image_b64, seq=2)).json()
    assert a["task_id"] != b["task_id"]
    assert b["duplicate"] is False
    assert len(_device_tasks(app, device_id)) == 2


def test_seq_claim_refuses_a_second_holder(app, client: TestClient, owner):
    """占住 seq 的机制本身：第二次 claim 必须被挡回去。

    这是防"并发重发各建一条 Task"的那道闸。真正的最窄竞态
    （两个请求挤在 SELECT 与 INSERT 之间）走的是 `IntegrityError` 分支，
    没法用 HTTP 稳定地构造出来；但两条路通向同一个保证 ——
    **同一个 seq 只允许一个执行者**，这里锁的就是这个保证。
    """
    _headers, device_id = _bind(client, owner)
    session = app.state.session_factory()
    try:
        device = session.get(Device, device_id)
        _row, existing = _claim_seq(session, device, 99)
        assert existing is None

        session2 = app.state.session_factory()
        try:
            with pytest.raises(Exception) as excinfo:
                _claim_seq(session2, session2.get(Device, device_id), 99)
            assert getattr(excinfo.value, "status_code", None) == 409
        finally:
            session2.close()
    finally:
        session.close()


def test_claim_is_reusable_after_the_lease_expires(app, client: TestClient, owner):
    """占住之后崩了，租约到期要允许接管 —— 否则这条 seq 永远补不回来。"""
    _headers, device_id = _bind(client, owner)
    session = app.state.session_factory()
    try:
        from datetime import timedelta

        device = session.get(Device, device_id)
        row, _ = _claim_seq(session, device, 5)
        row.created_at = utcnow() - timedelta(seconds=ingest_module._CLAIM_LEASE_S + 1)
        session.commit()

        _row, existing = _claim_seq(session, session.get(Device, device_id), 5)
        assert existing is None, "租约过期后应当能接管重做"
    finally:
        session.close()


def test_deleted_task_does_not_wedge_the_seq(app, client: TestClient, owner, image_b64):
    """用户在历史页把任务删了，盒子再重发同一条 seq 应该能**补回来**。

    这条守的是 `DeviceIngest.task_id` 上的 `ondelete="CASCADE"`。
    换成 `SET NULL` 的话，删任务会让台账变成"task_id 为空"——
    而那正是 `_claim_seq` 表示"有人正在处理"的信号，于是盒子会被告知
    "正在处理中，请稍后再试"，要等满一个租约，提示信息还是错的。

    注意断言里**不比 task_id**：SQLite 的主键是 rowid 别名（没有 AUTOINCREMENT），
    删掉 id=1 之后新建的那条**又会拿到 1**。所以"id 变了"是个假的判据。
    """
    headers, device_id = _bind(client, owner)
    first = _post(client, headers, _payload(image_b64, seq=11)).json()
    assert client.delete(f"/api/tasks/{first['task_id']}", headers=owner).status_code == 204

    # 任务被删 → 台账行随外键一起走，这个 seq 不再算"收过"
    session = app.state.session_factory()
    try:
        assert session.query(DeviceIngest).count() == 0
    finally:
        session.close()

    again = _post(client, headers, _payload(image_b64, seq=11))
    assert again.status_code == 200, again.text
    assert again.json()["duplicate"] is False
    assert again.json()["total_objects"] == 2
    # 重新落下了一条，而且只有一条
    assert len(_device_tasks(app, device_id)) == 1


# ---------------------------------------------------------------- 载荷防线
def test_missing_image_is_rejected(client: TestClient, owner):
    headers, _ = _bind(client, owner)
    response = _post(client, headers, _payload("", seq=1))
    assert response.status_code == 400
    assert "原图" in response.json()["detail"]


def test_broken_base64_is_400_not_500(client: TestClient, owner):
    headers, _ = _bind(client, owner)
    response = _post(client, headers, _payload("!!!not base64!!!", seq=1))
    assert response.status_code == 400


def test_non_image_bytes_are_400(client: TestClient, owner):
    headers, _ = _bind(client, owner)
    junk = base64.b64encode(b"this is definitely not a jpeg").decode("ascii")
    assert _post(client, headers, _payload(junk, seq=1)).status_code == 400


def test_oversized_image_is_413(settings, image_b64):
    """超过 storage.max_upload_mb 直接拒绝，而不是先解码进内存。

    **特意用一个 1MB 上限的独立应用。** 真实默认是 500MB，照它算长度会造出约
    700MB 的字符串、再由 httpx 的 `json=` 编码一遍（峰值接近 1.4GB）—— 而这条
    用例要证的只是"超限就 413"，上限多大并不影响结论。`test_security.py` 里的
    同类用例已经是这个做法（"避免为测试生成几百 MB 数据"）。
    """
    tiny = replace(settings, storage=replace(settings.storage, max_upload_mb=1))
    app = create_app(tiny)
    with TestClient(app) as client:
        creds = {"username": "tinyuploader", "password": "tiny123456"}
        client.post("/api/auth/register", json=creds)
        token = client.post("/api/auth/login", json=creds).json()["access_token"]
        headers, _ = _bind(client, {"Authorization": f"Bearer {token}"})

        # 1.4MB 的 base64 > 1MB 上限（4/3 是 base64 的膨胀比）
        huge = "A" * (1024 * 1024 * 4 // 3 + 4096)
        assert _post(client, headers, _payload(huge, seq=1)).status_code == 413


def test_too_many_detections_is_rejected(client: TestClient, owner, image_b64):
    headers, _ = _bind(client, owner)
    one = {"class_id": 0, "class_name": "aphid", "score": 0.5, "bbox": [0, 0, 1, 1]}
    response = _post(client, headers, _payload(image_b64, detections=[one] * 501))
    assert response.status_code == 422  # pydantic 的 max_length，不是 500


def test_zero_detections_is_fine(client: TestClient, app, owner, image_b64):
    """一个虫子都没拍到是一次**成功**的抓拍，不是错误。"""
    headers, device_id = _bind(client, owner)
    body = _post(client, headers, _payload(image_b64, detections=[]))
    assert body.status_code == 200
    assert body.json()["total_objects"] == 0
    assert body.json()["counts"] == {}


# ---------------------------------------------------------------- 归属
def test_device_id_filter_is_owner_checked(client: TestClient, owner, image_b64, register_user):
    """`device_id` 过滤参数不能变成窥探别人设备的旁路。"""
    headers, device_id = _bind(client, owner)
    _post(client, headers, _payload(image_b64))

    other = register_user("peeker")
    # 不按账号过滤**再**也是旁路：设备上报的任务 `user_id = device.owner_id`，
    # 所以别人的账号在一览里根本看不到它（见 services/ownership.py 的口径）。
    # 这条断言的另一半意义是挡住"只给 device_id 参数加了校验、裸列表还是全库"。
    assert client.get("/api/tasks", headers=other).json()["total"] == 0
    assert client.get("/api/stats/summary", headers=other).json()["total_tasks"] == 0

    # 但显式按设备过滤要过归属校验 —— 不是我的设备就是 404，不是 403
    assert client.get("/api/tasks", params={"device_id": device_id},
                      headers=other).status_code == 404
    assert client.get("/api/stats/summary", params={"device_id": device_id},
                      headers=other).status_code == 404

    # 设备的主人自己用则是正常的
    assert client.get("/api/tasks", params={"device_id": device_id},
                      headers=owner).json()["total"] == 1
    summary = client.get("/api/stats/summary", params={"device_id": device_id},
                         headers=owner).json()
    assert summary["total_tasks"] == 1
    assert summary["total_objects"] == 2


def test_device_filter_is_404_for_a_nonexistent_device(client: TestClient, owner):
    assert client.get("/api/tasks", params={"device_id": 999999},
                      headers=owner).status_code == 404
    assert client.get("/api/stats/summary", params={"device_id": 999999},
                      headers=owner).status_code == 404


def test_device_tasks_endpoint_sees_the_ingested_task(client: TestClient, owner, image_b64):
    headers, device_id = _bind(client, owner)
    posted = _post(client, headers, _payload(image_b64)).json()

    page = client.get(f"/api/devices/{device_id}/tasks", headers=owner).json()
    assert page["total"] == 1
    assert page["items"][0]["id"] == posted["task_id"]
    assert page["items"][0]["device_id"] == device_id


def test_task_carries_the_device_plot(client: TestClient, app, owner, image_b64):
    """设备默认地块 → 上报的任务自动落到它。这是目前唯一给盒子抓拍分地块的路径。"""
    plot = client.post("/api/plots", json={"name": "东头大棚", "crop": "番茄"},
                       headers=owner).json()
    headers, device_id = _bind(client, owner)
    client.patch(f"/api/devices/{device_id}", json={"plot_id": plot["id"]}, headers=owner)

    body = _post(client, headers, _payload(image_b64))
    assert client.get(f"/api/tasks/{body.json()['task_id']}", headers=owner).json()[
        "plot_id"
    ] == plot["id"]


# ---------------------------------------------------------------- 台账清理
def test_prune_ledger_drops_only_expired_rows(client: TestClient, app, owner, image_b64):
    """台账只覆盖重发窗口，不跟抓拍次数一起无限长。**只删台账，不删任务**。"""
    headers, device_id = _bind(client, owner)
    _post(client, headers, _payload(image_b64, seq=1))

    session = app.state.session_factory()
    try:
        settings = app.state.settings
        assert prune_ledger(session, settings) == 0, "刚写的台账不该被剪"

        from datetime import timedelta

        row = session.query(DeviceIngest).one()
        row.created_at = utcnow() - timedelta(days=settings.storage.retention_days + 1)
        session.commit()

        assert prune_ledger(session, settings) == 1
        assert session.query(DeviceIngest).count() == 0
        # 任务本身必须还在 —— 台账是幂等用的，不是数据
        assert session.query(Task).filter(Task.mode == TaskMode.DEVICE).count() == 1
    finally:
        session.close()
