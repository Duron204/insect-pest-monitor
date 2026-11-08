# -*- coding: utf-8 -*-
"""核查后端 Pydantic 契约与前端 TS 类型是否漂移。

**为什么需要它**：后端 `app/schemas.py` 与前端 `src/types/index.ts` 是两份手写的
契约，谁都不会在对方改的时候报错。字段被删、被改名、被加了新字段但前端没跟上，
都是"编译得过、运行起来某个格子永远是空"这种最难查的故障。

用法（在 backend/ 下）：
    python tools/check_contract_drift.py            # 报告差异
    python tools/check_contract_drift.py --strict  # 有差异就以非 0 退出（给 CI 用）
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_TYPES = BACKEND_ROOT.parent / "vue-frontend" / "src" / "types" / "index.ts"

#: 后端 Pydantic 名 -> 前端 TS 名（只有命名不同才需要列在这里）
NAME_MAP: dict[str, str] = {
    "Page": "PageOut",
    "ModelListOut": "ModelRegistryOut",
    "VideoAcceptedOut": "VideoTaskAcceptedOut",
    "DetectionOut": "Detection",
    "TokenOut": "LoginResponse",
    "MessageOut": "MessageResponse",
    "AvatarOut": "AvatarResponse",
}

#: 只存在于一侧、且是**有意为之**的字段（不报为漂移）
ACCEPTED_EXTRA: dict[str, frozenset[str]] = {
    # 前端内部/展示用字段，后端不该有
    "Detection": frozenset(),
}


def backend_fields() -> dict[str, dict[str, Any]]:
    sys.path.insert(0, str(BACKEND_ROOT))
    from app import schemas  # noqa: PLC0415

    out: dict[str, dict[str, Any]] = {}
    for name in dir(schemas):
        obj = getattr(schemas, name)
        if not isinstance(obj, type) or not issubclass(obj, schemas.BaseModel):
            continue
        if obj in (schemas.BaseModel, schemas.ORMModel):
            continue
        out[name] = {
            f: info.annotation for f, info in getattr(obj, "model_fields", {}).items()
        }
    return out


def _strip_comments(body: str) -> str:
    """去掉注释；再把**嵌套对象/数组字面量**的整块替换成占位符。

    不这么做的话，`host: { platform: string; ... }` 里那些内层成员会被当成
    外层 interface 的字段，凭空报出一堆"前端多字段"的假阳性。
    """
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    # 反复消掉最内层的对象字面量 {...}
    while True:
        new = re.sub(r"\{[^{}]*\}", "PLACEHOLDER", body)
        if new == body:
            break
        body = new
    # 只消掉**装着对象**的数组（元素类型是内联对象的数组）；
    # 像 `bbox: [number, number, number, number]` 这种元组是真实类型，必须保留
    while True:
        new = re.sub(r"\[[^\[\]]*PLACEHOLDER[^\[\]]*\]", "PLACEHOLDER", body)
        if new == body:
            break
        body = new
    return body


def _interface_bodies(text: str) -> dict[str, tuple[str, str]]:
    """返回 {接口名: (继承的父接口名 or "", 去掉外层括号后的成员体)}。"""
    out: dict[str, tuple[str, str]] = {}
    pattern = re.compile(
        r"export interface (\w+)(?:<[^>]*>)?(?:\s+extends\s+([\w<>,\s]+?))?\s*\{"
    )
    for m in pattern.finditer(text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        out[m.group(1)] = ((m.group(2) or "").strip(), text[m.end() : i - 1])
    return out


def frontend_fields() -> dict[str, dict[str, str]]:
    """粗解析 TS interface：抽出成员名，并沿 `extends` 链合并父接口的成员。"""
    text = FRONTEND_TYPES.read_text(encoding="utf-8")
    bodies = _interface_bodies(text)

    def parse(name: str, seen: frozenset[str] = frozenset()) -> dict[str, str]:
        if name not in bodies or name in seen:
            return {}
        parent, body = bodies[name]
        fields: dict[str, str] = {}
        # 先合并父接口：否则 `TaskDetailOut extends TaskOut` 会被误报成
        # "前端缺了 TaskOut 的全部字段"
        for p in re.findall(r"\w+", parent):
            fields.update(parse(p, seen | {name}))
        for line in _strip_comments(body).splitlines():
            line = line.strip()
            if not line:
                continue
            fm = re.match(r"([A-Za-z_]\w*)\??\s*:\s*(.+?);?$", line)
            if fm and fm.group(2) != "PLACEHOLDER":
                fields[fm.group(1)] = fm.group(2).strip()
        return fields

    return {name: parse(name) for name in bodies}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true", help="有差异时以非 0 退出")
    args = parser.parse_args(argv)

    if not FRONTEND_TYPES.is_file():
        print(f"找不到前端类型文件：{FRONTEND_TYPES}")
        return 2

    be, fe = backend_fields(), frontend_fields()
    fe_by_be_name = {NAME_MAP.get(k, k): v for k, v in fe.items()}

    drift = 0
    for be_name, be_fields in sorted(be.items()):
        ts_name = NAME_MAP.get(be_name, be_name)
        fe_fields = fe_by_be_name.get(ts_name)
        if fe_fields is None:
            continue  # 请求体/内部模型，前端不必有对应 interface

        missing = sorted(set(be_fields) - set(fe_fields) - ACCEPTED_EXTRA.get(ts_name, frozenset()))
        extra = sorted(set(fe_fields) - set(be_fields))

        if missing or extra:
            drift += 1
            print(f"\n[漂移] {be_name}" + (f"  →  前端 {ts_name}" if ts_name != be_name else ""))
            for f in missing:
                print(f"    - 前端缺字段：{f}  （后端 {be_fields[f]}）")
            for f in extra:
                print(f"    + 前端多字段：{f}  （后端没有；类型 {fe_fields[f]}）")

    print()
    if drift == 0:
        print("契约一致：后端每个响应模型的字段都能在前端类型里找到。")
        return 0
    print(f"共 {drift} 个模型存在字段漂移。")
    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
