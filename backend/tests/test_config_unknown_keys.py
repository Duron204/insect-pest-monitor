# -*- coding: utf-8 -*-
"""配置项拼写校验。

配置里一个拼错的键**不会报错**：`app.config._sub()` 为了"平滑演进"会把它静默丢掉，
于是你以为改了配置，实际一直在跑默认值。这类故障没有任何日志、没有异常，
只能靠"配置和行为对不上"的直觉去怀疑 —— 非常难查。

所以这里守住两件事：
  1. 仓库里**现有的** config.yaml / deploy/config.pi.yaml 不能有拼错的键；
  2. `unknown_config_keys()` 本身真的挑得出错键（否则上面第 1 条就是摆设）。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from app.config import unknown_config_keys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
#: 仓库里**随代码发布**的配置文件。用户在现场自己改的那份不归这里管。
SHIPPED_CONFIGS = [
    PROJECT_ROOT / "config.yaml",
    PROJECT_ROOT / "deploy" / "config.pi.yaml",
]


@pytest.mark.parametrize("path", SHIPPED_CONFIGS, ids=lambda p: p.name)
def test_shipped_configs_have_no_misspelled_keys(path):
    """随代码发布的配置里，每个键都得是加载器认得的。"""
    if not path.exists():
        pytest.skip(f"配置文件不存在：{path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    bad = unknown_config_keys(raw)
    assert not bad, (
        f"{path.name} 里有加载器不认识的键（会被静默忽略、回落到默认值）："
        + ", ".join(bad)
    )


def test_a_misspelled_nested_key_is_caught():
    """`agent.heartbeat_s` 写成 `agent.heartbeat_seconds` 必须被挑出来。"""
    bad = unknown_config_keys({"agent": {"enabled": True, "heartbeat_seconds": 5}})
    assert bad == ["agent.heartbeat_seconds"]


def test_an_unknown_top_level_key_is_caught():
    bad = unknown_config_keys({"storag": {"outputs_dir": "x"}})
    assert bad == ["storag"]


def test_a_well_formed_config_is_clean():
    """反过来：一份规规矩矩的配置不该报任何东西（防误报）。"""
    assert unknown_config_keys(
        {
            "server": {"host": "0.0.0.0", "port": 8000},
            "storage": {"retention_days": 30},
            "upload_policy": "on_detect",
        }
    ) == []


def test_non_mapping_input_is_tolerated():
    """YAML 文件为空 / 顶层不是映射时，safe_load 会给出 None 或标量。"""
    assert unknown_config_keys(None) == []
    assert unknown_config_keys("just a string") == []


def test_load_settings_warns_about_misspelled_keys(tmp_path, caplog):
    """拼错的键必须**在启动日志里喊出来**，不能只是内部算出来了事。"""
    from app.config import load_settings

    bad_config = tmp_path / "config.yaml"
    bad_config.write_text("agent:\n  heartbeat_seconds: 5\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="insect.config"):
        load_settings(bad_config)

    assert any("heartbeat_seconds" in r.getMessage() for r in caplog.records), (
        "拼错的配置键没有出现在日志里：现场只会看到'改了没生效'，查无可查"
    )
