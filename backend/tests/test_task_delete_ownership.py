# -*- coding: utf-8 -*-
"""删除任务的归属边界：**看得见 = 能删**。

为什么单独一个文件，而不是并进 `test_detection_api.py`：那边有模块级的
`pytestmark`，没有权重时会**整文件跳过** —— 而这条是安全边界，必须总是跑到。
这里不做任何推理，直接造任务行，毫秒级。

规则（定义在 `services/ownership.py`，读与删共用同一个判据）：
无主的任务谁都能看见、谁都能删（本机抓拍，历史遗留），有主的只有本人能看见/能删。

**这一版与上一版的区别**：上一版读是全局共享的，所以"删除别人有主的任务"回 403
并附一句说明（任务就在你的列表里，装不存在是撒谎）。现在读也收口了，别人有主的
任务在任何界面上都不出现 —— 再回 403 就等于承认"有这么一条，只是不是你的"，
接口成了按 id 枚举别人数据的探针。所以统一成 404，与设备接口同一口径。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models import Task, TaskMode, TaskStatus, User

#: 有主的任务：模拟"某个账号上传的单图检测"
OWNED = TaskMode.IMAGE
#: 无主的任务：本机摄像头抓拍写的就是 `user_id=NULL`
OWNERLESS = TaskMode.LOCAL


@pytest.fixture()
def user_a(register_user) -> dict[str, str]:
    return register_user("farmer")


@pytest.fixture()
def user_b(register_user) -> dict[str, str]:
    return register_user("neighbour")


def _make_task(app, user_id: int | None, mode: str = OWNED) -> int:
    """直接造一条已完结的任务行。

    不走上传接口：这里要证的只是"删除接口认不认归属"，
    真跑一次推理只会让用例慢几十秒，还会把它挂到权重上。
    """
    session = app.state.session_factory()
    try:
        task = Task(
            mode=mode,
            status=TaskStatus.DONE,
            source_name="sample.jpg",
            user_id=user_id,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        return task.id
    finally:
        session.close()


def _uid(app, username: str) -> int:
    """按用户名查出用户 id。

    刻意不写死 `user_id=1`（"第一个注册的就是它"）：那是个隐式依赖，
    将来夹具顺序一变，这条用例会以"403 变成了 204"的形式红掉，而看代码的人
    完全看不出是 id 对不上。查一次库，把这个假设显式化。
    """
    session = app.state.session_factory()
    try:
        return session.query(User).filter(User.username == username).one().id
    finally:
        session.close()


def test_a_foreign_task_is_invisible_and_cannot_be_deleted(
    client: TestClient, app, user_a, user_b
):
    """别人上传的任务：**看不见**，自然也删不了。

    这条是可证伪的反面护栏。它同时钉两件事：读过滤生效（GET 404），以及删除
    没有另一套更松的判据（DELETE 也是 404）。只断言 DELETE 的话，把读过滤写漏
    了也照样绿 —— 而"列表里看得见、删不掉"恰恰是上一版的形状，现在不该存在。
    """
    task_id = _make_task(app, user_id=_uid(app, "farmer"))

    # B 看不见（与"别人有主的任务在我这里等于不存在"同一口径）
    assert client.get(f"/api/tasks/{task_id}", headers=user_b).status_code == 404
    assert client.get("/api/tasks", headers=user_b).json()["total"] == 0
    # 也不能删。404 而不是 403：区别开就等于确认"这条存在，只是不是你的"，
    # 接口成了按 id 枚举别人数据量的探针。
    assert client.delete(f"/api/tasks/{task_id}", headers=user_b).status_code == 404

    # 404 必须是"什么都没做"：任务还在，A 自己照样看得见、删得掉
    assert client.get(f"/api/tasks/{task_id}", headers=user_a).status_code == 200
    assert client.delete(f"/api/tasks/{task_id}", headers=user_a).status_code == 204
    assert client.get(f"/api/tasks/{task_id}", headers=user_a).status_code == 404


def test_an_ownerless_task_can_be_deleted_by_anyone(client: TestClient, app, user_b):
    """无主的任务（本机抓拍）谁都能删 —— 它是农场的公共数据，没有"别人"可冒犯。

    这条挡住的是**过度收紧**：把规则写成"只有 `user_id == 我` 才能删"的话，
    本机摄像头抓的那一大堆（`user_id` 全是 NULL）就没人能删了 ——
    一个自己好不了的坑，而且症状是"点了删除没反应"，很难联想到是这个原因。
    """
    task_id = _make_task(app, user_id=None, mode=OWNERLESS)

    assert client.delete(f"/api/tasks/{task_id}", headers=user_b).status_code == 204
    assert client.get(f"/api/tasks/{task_id}", headers=user_b).status_code == 404


def test_deleting_a_task_that_does_not_exist_is_still_404(
    client: TestClient, user_a
):
    """归属校验不能把"不存在"变成 403 —— 那是两件事，报错要分得清。"""
    assert client.delete("/api/tasks/999999", headers=user_a).status_code == 404


def test_delete_still_requires_a_token(client: TestClient, app):
    task_id = _make_task(app, user_id=None, mode=OWNERLESS)
    assert client.delete(f"/api/tasks/{task_id}").status_code == 401
