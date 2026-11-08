# -*- coding: utf-8 -*-
"""`/media/outputs` 的媒体票据闸门。

为什么这个文件必须存在：`/media/outputs` 是 `StaticFiles` 挂载，**请求不经过
FastAPI 的依赖注入**。如果只给接口加了归属过滤、没管这个目录，别的账号数一数
任务 id 就能把 `original.jpg` / `annotated.jpg` 全下走 —— "账号分开"只做了一半。
`app/media_guard.py` 就是那道闸，这里钉住它的每一条边界。

不跑推理、不建任务行：这里测的是"地址能不能取到文件"，与业务无关。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.media_guard import task_id_from_path
from app.security import create_access_token, new_stream_ticket, new_task_media_ticket

#: StaticFiles 只看后缀与文件是否存在，内容是什么无所谓。
_FAKE_JPEG = b"\xff\xd8\xff\xe0 not really a jpeg, but StaticFiles does not care"


@pytest.fixture()
def outputs(settings: Settings) -> Path:
    """在临时产物目录里摆两个任务的文件，含一条批量检测的嵌套路径。"""
    root = settings.abs_outputs_dir
    for task_id in (1, 2):
        directory = root / f"{task_id:06d}"
        (directory / "batch").mkdir(parents=True, exist_ok=True)
        (directory / "annotated.jpg").write_bytes(_FAKE_JPEG)
        (directory / "batch" / f"{task_id:03d}_sample_orig.jpg").write_bytes(_FAKE_JPEG)
    return root


def _ticket(settings: Settings, task_id: int, user_id: int = 1) -> str:
    return new_task_media_ticket(
        task_id, user_id, settings.resolved_jwt_secret,
        algorithm=settings.auth.algorithm,
    )


# ---------------------------------------------------------------- 路径解析
@pytest.mark.parametrize(
    "path",
    [
        "/media/outputs/000123/../000124/annotated.jpg",   # 借 .. 换任务号
        "/media/outputs/000123/./annotated.jpg",           # 单点段同样可疑
        "/media/outputs/000123\\..\\000124\\a.jpg",         # Windows 分隔符
        "/media/outputs/000123",                           # 只有目录、没有文件名
        # 注意：`/media/outputs/000123/`（带尾斜杠）**能**解析出 123 —— 这不是漏洞：
        # 它拿到的是"自己那张票"能看的目录，StaticFiles 在没有 index 时回 404，
        # 读不到任何东西。真正要挡的是"借路径换任务号"，上面第一条已经挡住。
        "/media/outputs/abc/annotated.jpg",                # 任务号不是数字
        "/media/outputs//annotated.jpg",                   # 空的任务号段
    ],
)
def test_suspicious_paths_yield_no_task_id(path):
    """形状可疑的地址一律解析不出任务号 —— 拿不到号就拿不到票，请求止步 401。"""
    assert task_id_from_path(path) is None


def test_superscript_digits_are_not_task_ids():
    """`isdigit()` 对上标数字（"²"）返回 True，但 `int("²")` 会抛 ValueError。

    若这里用了 `isdigit`，一个畸形路径会变成 500（服务端异常）而不是 401，
    等于给了外人一个探测服务端行为差异的口子。
    """
    assert task_id_from_path("/media/outputs/\u00b2/annotated.jpg") is None


def test_a_normal_path_still_resolves():
    """防误报：正常的地址必须照常解析出任务号，否则闸门就成了"全拒"。

    批量检测还有 `batch/xxx_orig.jpg` 这种嵌套路径，也要能认。
    """
    assert task_id_from_path("/media/outputs/000123/annotated.jpg") == 123
    assert task_id_from_path("/media/outputs/000123/batch/001_sample_orig.jpg") == 123


# ---------------------------------------------------------------- 拒绝
def test_no_ticket_is_401(client: TestClient, outputs):
    """裸地址（旧接口、书签、爬虫）一律拒绝。"""
    response = client.get("/media/outputs/000001/annotated.jpg")
    assert response.status_code == 401
    assert "刷新" in response.json()["detail"]
    # 票据会随 URL 外泄：连拒绝响应也不该让浏览器把地址带给外站
    assert response.headers["referrer-policy"] == "no-referrer"


def test_the_401_carries_cors_headers(client: TestClient, outputs):
    """闸门必须**在 CORS 里面**（main.py 的注册顺序）。

    反过来的话，开发期前端（Vite 端口跨域）收到的 401 没有 CORS 头，
    浏览器只会报 "CORS error"，真正的原因（票据过期）被藏起来 —— 现场极难查。
    """
    response = client.get(
        "/media/outputs/000001/annotated.jpg",
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_a_ticket_for_another_task_is_401(client: TestClient, outputs, settings: Settings):
    """票据按任务隔离 —— 这是"拿到一条任务的地址"不等于"拿到全部"的关键。"""
    response = client.get(f"/media/outputs/000002/annotated.jpg?mt={_ticket(settings, 1)}")
    assert response.status_code == 401


def test_an_expired_ticket_is_401(client: TestClient, outputs, settings: Settings):
    expired, _ = create_access_token(
        "1",
        settings.resolved_jwt_secret,
        expires_seconds=-10,
        algorithm=settings.auth.algorithm,
        extra={"purpose": "task_media", "task": 1},
    )
    assert client.get(f"/media/outputs/000001/annotated.jpg?mt={expired}").status_code == 401


def test_a_user_jwt_cannot_be_used_as_a_ticket(client: TestClient, outputs, settings: Settings):
    """两种令牌同一个密钥签发，所以用途检查**必须**在闸门里再做一遍。"""
    login, _ = create_access_token(
        "1", settings.resolved_jwt_secret, algorithm=settings.auth.algorithm
    )
    assert client.get(f"/media/outputs/000001/annotated.jpg?mt={login}").status_code == 401


def test_a_stream_ticket_cannot_be_used_as_a_media_ticket(
    client: TestClient, outputs, settings: Settings
):
    """用途互不相通：能看摄像头 ≠ 能取检测产物。"""
    stream, _ = new_stream_ticket(
        1, 1, settings.resolved_jwt_secret, ttl_s=120
    )
    assert client.get(f"/media/outputs/000001/annotated.jpg?mt={stream}").status_code == 401


def test_dot_segments_cannot_borrow_a_ticket(
    client: TestClient, outputs, settings: Settings
):
    """一枚任务 1 的票据 + `..` 不能读到任务 2 —— 这是 StaticFiles 的 normpath 旁路。

    直接测 `task_id_from_path`：`..` 在 HTTP 客户端/URL 库的规范化下未必能原样
    打到服务端，而这条安全性质本身不依赖某一种编码。再补一次 HTTP 断言，
    确认编码形式同样被挡。
    """
    assert task_id_from_path("/media/outputs/000001/../000002/annotated.jpg") is None
    assert task_id_from_path("/media/outputs/000001/..\\000002/annotated.jpg") is None
    # 畸形任务段要变成 401，而不是在 int() 上抛异常变成 500
    assert task_id_from_path("/media/outputs/²²²/annotated.jpg") is None
    assert task_id_from_path("/media/outputs/abc/annotated.jpg") is None

    ticket = _ticket(settings, 1)
    encoded = client.get(
        f"/media/outputs/000001/%2e%2e/000002/annotated.jpg?mt={ticket}"
    )
    assert encoded.status_code == 401


# ---------------------------------------------------------------- 放行
def test_a_valid_ticket_serves_the_file(client: TestClient, outputs, settings: Settings):
    """验票要**确实能过** —— 只测拒绝的话，一个永远 401 的实现也会全绿。"""
    response = client.get(f"/media/outputs/000001/annotated.jpg?mt={_ticket(settings, 1)}")
    assert response.status_code == 200
    assert response.content == _FAKE_JPEG
    assert response.headers["referrer-policy"] == "no-referrer"


def test_nested_batch_paths_use_the_same_ticket(client: TestClient, outputs, settings: Settings):
    """批量结果是 `batch/xxx_orig.jpg`：票据只看任务段，不看文件层级。"""
    path = "/media/outputs/000001/batch/001_sample_orig.jpg"
    assert client.get(f"{path}?mt={_ticket(settings, 1)}").status_code == 200
    assert client.get(path).status_code == 401


def test_avatars_are_still_public(client: TestClient, settings: Settings):
    """头像不属于识别数据，刻意不设闸 —— 这一条防止"顺手全锁上"。"""
    avatars = settings.data_dir / "avatars"
    avatars.mkdir(parents=True, exist_ok=True)
    (avatars / "me.png").write_bytes(b"png")
    assert client.get("/media/avatars/me.png").status_code == 200


# ---------------------------------------------------------------- 缓存
def test_a_ticket_is_stable_within_its_window(settings: Settings):
    """同一个 (任务, 账号) 在同一窗口里必须签出同一串。

    否则接口每返回一次地址就变一次，浏览器缓存整页作废 —— 历史页翻一页要重下
    20 张缩略图。见 `security.new_task_media_ticket` 的说明。
    """
    first = _ticket(settings, 7, user_id=3)
    second = _ticket(settings, 7, user_id=3)
    assert first == second
    # 换个账号就是另一串（窗口与任务都相同也一样）
    assert _ticket(settings, 7, user_id=4) != first
