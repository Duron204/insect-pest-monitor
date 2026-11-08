# -*- coding: utf-8 -*-
"""把「前后端契约漂移」变成会红的测试。

后端 `app/schemas.py` 和前端 `src/types/index.ts` 是两份**手写**的契约，
改一个不会让另一个报错。字段被删、被改名、或者后端加了新字段前端没跟上，
表现都是"编译得过、运行起来某个格子永远空着"—— 这类故障靠人眼 review 抓不住。

这里直接跑 `tools/check_contract_drift.py`，让漂移在 CI 上红出来。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
CHECKER = BACKEND_ROOT / "tools" / "check_contract_drift.py"


def test_backend_and_frontend_types_do_not_drift():
    """后端每个响应模型的字段，前端类型里都得有对应声明。"""
    if not CHECKER.is_file():
        pytest.fail(f"找不到契约检查脚本：{CHECKER}")

    proc = subprocess.run(
        [sys.executable, str(CHECKER), "--strict"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(BACKEND_ROOT),
    )
    assert proc.returncode == 0, (
        "后端 Pydantic 模型与前端 TS 类型不一致（改动一边忘了另一边）：\n"
        f"{proc.stdout}{proc.stderr}"
    )


def test_the_checker_itself_finds_a_deliberate_drift(tmp_path):
    """自检：确认上面那条测试**真的会红**，不是永远通过的摆设。

    造一份故意少字段的前端类型，喂给检查器；如果它还报 0 漂移，
    说明解析器坏了（比如把整个 interface 体吞了），上面那条断言也就失去意义。
    """
    fake_types = tmp_path / "index.ts"
    fake_types.write_text(
        "export interface HealthOut {\n"
        "  status: string\n"
        "  version: string\n"
        "}\n",
        encoding="utf-8",
    )

    script = (
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, r'{BACKEND_ROOT / 'tools'}')\n"
        "import check_contract_drift as c\n"
        f"c.FRONTEND_TYPES = Path(r'{fake_types}')\n"
        "print('EXIT', c.main(['--strict']))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(BACKEND_ROOT),
    )
    # 后端 HealthOut 有 device/gpu_available/... 等字段，前端只写了两个 → 必须报漂移
    assert "EXIT 1" in proc.stdout, (
        "检查器对一份明显缺字段的类型文件没报漂移，说明它已经失效：\n"
        f"{proc.stdout}{proc.stderr}"
    )
