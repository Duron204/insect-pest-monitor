# -*- coding: utf-8 -*-
"""归属规则：**有主的只有主人看得见，无主的所有人都看得见。看得见 = 能删。**

这个文件存在的唯一理由，是让这条规则**只写一遍**。它同时是「读」与「删」的判据，
散在四个路由里各写一个 `if`，迟早会写出四个略有差别的版本 —— 而差一点点的那一个
就是漏洞。

**为什么无主（`user_id IS NULL`）要对所有账号可见，而不是"谁都别看见"**：
本机摄像头定时抓拍（`services/local_camera.py`）压根没有"谁上传的"这一说，
它写的就是 NULL；更早的历史数据也是 NULL。若写成 `col == me`，那一大批抓拍会从
**所有人的**看板、历史、告警里整体消失 —— 那个症状看起来是"数据没了"，
很难让人联想到"隔离生效了"。所以口径是**无主 = 农场的公共数据**：谁都能看、
谁都能删，没有"别的账号"可以被冒犯。

**为什么不是自己的回 404 而不是 403**：上一轮删除接口是唯一的例外 —— 当时读是
全局共享的，任务就摆在 B 的列表里，回 404 等于撒一个立刻能被拆穿的谎，所以回
403 并附一句说明。现在**读也收口了**：别人有主的任务不再出现在任何界面上。
这时再回 403（"有这么一条，只是不是你的"）就把接口变成了一个**按 id 枚举别人
数据量**的探针 —— 与 `routers/devices.py::_owned_device_or_404` 当初拒绝泄露
"这台设备存不存在"是同一个理由。所以两边统一：**看不见就等于不存在**。

刻意不做的事：

- **没有管理员 / 角色。** `User` 没有 `role` 列，而"无主的公共"已经覆盖了
  "系统自己产生的数据"这一类，不需要一个超级账号。
- **不区分读与写。** 看得见的就能删。例外只有一处：删除会连带抹掉产物目录且
  不可撤销 —— 但那不是权限问题，是**没有别的人能看见**，所以它自然只有主人能做。
- **不覆盖服务器资源。** 模型清单、虫种知识库、主机状态是这台机器上的软件资源，
  不是哪个账号的数据，它们仍然共享（这是用户明确选定的口径）。
"""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models import User


def owner_scope(column, user: User):
    """`WHERE` 谓词：**无主的 + 归我的**。所有按账号过滤的查询都从这里取。

    参数是**列**而不是模型，因为 `Task` 上那列叫 `user_id`、`Alert` / `Plot`
    上叫 `owner_id`：调用方各自传自己的列，规则本身仍然只有这一份。
    """
    return or_(column == user.id, column.is_(None))


def owned_or_404(
    db: Session,
    model,
    obj_id: int,
    user: User,
    *,
    owner_attr: str,
    what: str,
):
    """取一条**调用者看得见**的行（自己的或同时无主的），否则 404。

    `owner_attr` 是必填的关键字参数，不给默认值：默认值会让"Task 到底用哪一列"
    变成靠记性的事，而写错列名的后果是**过滤静默失效**（所有行都"看得见"）。
    调用方必须明写 `owner_attr="user_id"` 或 `"owner_id"`。
    """
    row = db.get(model, obj_id)
    if row is None or not _is_visible(row, user, owner_attr):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"{what}不存在"
        )
    return row


def _is_visible(row, user: User, owner_attr: str) -> bool:
    owner_id = getattr(row, owner_attr)
    return owner_id is None or owner_id == user.id
