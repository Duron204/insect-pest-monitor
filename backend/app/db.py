# -*- coding: utf-8 -*-
"""数据库引擎与会话。

要点：
- SQLite 开 WAL：读多写少、且推理线程与请求线程并发访问时更稳。
- check_same_thread=False：推理跑在线程池里，连接会跨线程使用。
- 外键约束显式打开（SQLite 默认关闭），否则级联删除不生效。
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger("insect.db")


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def init_engine(url: str, echo: bool = False) -> Engine:
    """初始化全局引擎与会话工厂。可重复调用（会重建）。"""
    global _engine, _SessionLocal

    connect_args: dict = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    _engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)

    if url.startswith("sqlite"):

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

    _SessionLocal = sessionmaker(
        bind=_engine, autoflush=False, expire_on_commit=False, class_=Session
    )
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("数据库引擎尚未初始化，请先调用 init_engine()")
    return _engine


def session_factory() -> sessionmaker[Session]:
    """返回 sessionmaker（**是工厂，不是 Session**）。

    易踩坑：`session_factory()` 得到的是 maker，必须再调用一次才是 Session。
    为免误用，新代码请直接用 `new_session()`。
    """
    if _SessionLocal is None:
        raise RuntimeError("会话工厂尚未初始化，请先调用 init_engine()")
    return _SessionLocal


def new_session() -> Session:
    """直接取得一个 Session。"""
    maker = session_factory()
    return maker()


def create_all() -> None:
    """建表（幂等）。"""
    # 必须先 import 模型，否则 Base.metadata 里没有表定义
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=get_engine())


def add_missing_columns() -> list[str]:
    """给**已存在**的表补上模型里新加的列，返回补上的 `表.列` 清单。

    `create_all()` 只建**缺失的表**，绝不给已存在的表补列。于是"模型加了字段、
    库没迁移"会以最难看的形式炸出来：SQLAlchemy 查询整行时报
    `no such column`，设备列表整个 500 —— 而且是在**已经拷到树莓派上、
    重启之后**才炸。这台项目的部署方式是"把文件夹拷过去再重启"，没人会记得
    先跑一次迁移脚本，所以这一步必须在启动时自己做完。

    只处理**可空、且没有默认值**的列：这类 `ALTER TABLE ADD COLUMN` 是 SQLite
    原生支持的，不碰任何既有数据、不需要整表重建。带 NOT NULL 或默认值的列
    一律跳过并告警 —— 那属于需要重建表的迁移，交给 `tools/` 下的脚本人工确认。
    """
    from . import models  # noqa: F401

    engine = get_engine()
    inspector = inspect(engine)
    added: list[str] = []

    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue  # create_all 刚把它建好，列是齐的
        present = {column["name"] for column in inspector.get_columns(table.name)}
        pending: list[str] = []
        for column in table.columns:
            if column.name in present:
                continue
            if not column.nullable or column.default is not None or column.server_default is not None:
                logger.warning(
                    "表 %s 缺列 %s，但它非空或带默认值，启动时不敢自动补 —— 请用 tools/ 下的迁移脚本处理",
                    table.name,
                    column.name,
                )
                continue
            pending.append(f"ADD COLUMN {column.name} {column.type}")
        if not pending:
            continue
        with engine.begin() as connection:
            for ddl in pending:
                connection.execute(text(f"ALTER TABLE {table.name} {ddl}"))
        for ddl in pending:
            name = ddl.split()[2]
            added.append(f"{table.name}.{name}")
            logger.info("已为既有表补列：%s.%s", table.name, name)

    return added


def drop_all() -> None:
    from . import models  # noqa: F401

    Base.metadata.drop_all(bind=get_engine())
