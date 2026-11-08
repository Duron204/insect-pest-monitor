# -*- coding: utf-8 -*-
"""安全相关测试：路径穿越、上传校验、体积限制、令牌不外泄。

这些断言对应的都是**真实攻击面**，不是形式化检查：
- 文件名来自设备与浏览器，一律视为不可信输入，绝不能拼出工作目录之外的路径；
- 扩展名可以随便改，所以图片必须**按内容**校验，而不是信后缀；
- 体积限制必须在服务端生效，否则一个请求就能打满磁盘。
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import sys

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import _within, create_app
from app.services.storage import safe_filename, save_upload

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_IMAGE = PROJECT_ROOT / "test_media" / "InsectPest_SIXU_A00025.jpg"


# ---------------------------------------------------------------- 文件名清洗
def test_safe_filename_strips_directory_traversal():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename(r"..\..\windows\win.ini") == "win.ini"
    assert safe_filename("C:/Windows/System32/cmd.exe") == "cmd.exe"
    assert safe_filename("/absolute/path/file.jpg") == "file.jpg"


def test_safe_filename_handles_empty_and_none():
    assert safe_filename("") == "file"
    assert safe_filename(None) == "file"
    assert safe_filename("...") == "file"          # 全是点号会被清空
    assert safe_filename("///") == "file"


def test_safe_filename_replaces_dangerous_characters():
    cleaned = safe_filename('a*b?c"d|e<f>g.jpg')
    for bad in '*?"|<>':
        assert bad not in cleaned
    assert cleaned.endswith(".jpg")


def test_safe_filename_keeps_chinese_names():
    """现场文件名很可能是中文，不应被清洗掉。"""
    assert safe_filename("东地块_2026-10-09.jpg") == "东地块_2026-10-09.jpg"


def test_safe_filename_is_length_limited():
    assert len(safe_filename("x" * 500 + ".jpg")) <= 120


def test_save_upload_cannot_escape_uploads_dir(settings):
    path = save_upload(settings, "../../escape.jpg", b"payload")
    uploads = settings.abs_uploads_dir.resolve()
    assert path.resolve().parent == uploads, "上传文件必须落在 uploads 目录内"
    assert path.exists()
    path.unlink()


# ---------------------------------------------------------------- 上传校验
def test_avatar_rejects_content_that_is_not_an_image(loaded_app_auth):
    client = loaded_app_auth
    response = client.post(
        "/api/auth/avatar",
        files={"file": ("evil.png", b"<html>definitely not an image</html>", "image/png")},
    )
    # 扩展名合法但内容不是图片 —— 必须按内容拒绝
    assert response.status_code == 400
    assert "图片" in response.json()["detail"]


def test_avatar_rejects_disallowed_extension(loaded_app_auth):
    response = loaded_app_auth.post(
        "/api/auth/avatar",
        files={"file": ("payload.exe", b"MZ\x90\x00", "application/octet-stream")},
    )
    assert response.status_code == 400


def test_avatar_accepts_real_image(loaded_app_auth):
    with TEST_IMAGE.open("rb") as handle:
        response = loaded_app_auth.post(
            "/api/auth/avatar", files={"file": ("a.jpg", handle, "image/jpeg")}
        )
    assert response.status_code == 200, response.text
    assert response.json()["avatar_path"].startswith("/media/avatars/")


def test_oversized_avatar_is_413(loaded_app_auth):
    """超过 2MB 直接 413。

    值上限本身很小，所以这条用例额外守住的是**上传是边读边数、超限即断**这条路径：
    改成"先 `await file.read()` 再比大小"的话，限制的就只是"允不允许存" ——
    内存早就吃完了，在 1~2GB 的树莓派上这个 413 就是那次 OOM 的遗言。
    """
    response = loaded_app_auth.post(
        "/api/auth/avatar",
        files={"file": ("big.jpg", b"\xff" * (3 * 1024 * 1024), "image/jpeg")},
    )
    assert response.status_code == 413, response.text
    assert "2 MB" in response.json()["detail"]


def test_model_upload_rejects_non_pt_file(loaded_app_auth):
    response = loaded_app_auth.post(
        "/api/models/upload",
        files={"file": ("model.onnx", b"x" * 4096, "application/octet-stream")},
    )
    assert response.status_code == 400
    assert "pt" in response.json()["detail"].lower()


def test_model_upload_rejects_tiny_file(loaded_app_auth):
    """小于 1KB 的 .pt 基本不可能是有效权重，直接拒绝而不是交给 torch 去炸。"""
    response = loaded_app_auth.post(
        "/api/models/upload",
        files={"file": ("tiny.pt", b"not a real weight", "application/octet-stream")},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------- 体积限制
def test_upload_size_limit_is_enforced_server_side(tmp_path):
    """用一个 1MB 上限的独立应用验证 413，避免为测试生成几百 MB 数据。"""
    base = load_settings()
    config = replace(
        base,
        data_dir=tmp_path / "data",
        server=replace(base.server, serve_frontend=False),
        database=replace(base.database, url=f"sqlite:///{(tmp_path / 'limit.db').as_posix()}"),
        storage=replace(
            base.storage,
            outputs_dir=str(tmp_path / "outputs"),
            uploads_dir=str(tmp_path / "uploads"),
            max_upload_mb=1,
        ),
        inference=replace(base.inference, preload=False),
        auth=replace(base.auth, jwt_secret="test-secret-key-for-pytest-only-0123456789"),
    )
    app = create_app(config)
    with TestClient(app) as client:
        client.post("/api/auth/register", json={"username": "lim", "password": "secret123"})
        token = client.post(
            "/api/auth/login", json={"username": "lim", "password": "secret123"}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        oversized = b"\xff" * (2 * 1024 * 1024)   # 2MB > 1MB 上限
        response = client.post(
            "/api/detection/image",
            files={"file": ("big.jpg", oversized, "image/jpeg")},
            headers=headers,
        )
        assert response.status_code == 413


def test_batch_file_count_limit_is_enforced(tmp_path):
    base = load_settings()
    config = replace(
        base,
        data_dir=tmp_path / "data",
        server=replace(base.server, serve_frontend=False),
        database=replace(base.database, url=f"sqlite:///{(tmp_path / 'batch.db').as_posix()}"),
        storage=replace(
            base.storage,
            outputs_dir=str(tmp_path / "outputs"),
            uploads_dir=str(tmp_path / "uploads"),
            max_batch_files=2,
        ),
        inference=replace(base.inference, preload=False),
        auth=replace(base.auth, jwt_secret="test-secret-key-for-pytest-only-0123456789"),
    )
    app = create_app(config)
    with TestClient(app) as client:
        client.post("/api/auth/register", json={"username": "bat", "password": "secret123"})
        token = client.post(
            "/api/auth/login", json={"username": "bat", "password": "secret123"}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        files = [("files", (f"a{i}.jpg", b"\xff\xd8\xff", "image/jpeg")) for i in range(3)]
        response = client.post("/api/detection/batch", files=files, headers=headers)
        assert response.status_code == 413


# ---------------------------------------------------------------- 越权
def test_health_is_public_but_others_are_not(client):
    assert client.get("/api/health").status_code == 200
    for path in ("/api/tasks", "/api/models", "/api/species", "/api/stats/summary"):
        assert client.get(path).status_code == 401, path


# ---------------------------------------------------------------- 404 与 405 的语义
def test_unknown_path_returns_404(client):
    """路径不存在应是 404（本夹具关闭了前端托管，故不含 SPA 回退）。"""
    assert client.post("/detection/image").status_code == 404   # 故意漏掉 /api 前缀
    assert client.post("/api/nope/nope").status_code == 404


def test_wrong_method_on_real_path_reports_405_with_allowed_methods(client):
    """路径存在但方法不对，仍应是 405，并列出该路径允许的方法。"""
    response = client.post("/api/tasks")        # /api/tasks 只有 GET
    assert response.status_code == 405
    detail = response.json()["detail"]
    assert "POST" in detail and "GET" in detail, detail
    assert client.delete("/api/models").status_code == 405   # 存在但不支持 DELETE


@pytest.fixture()
def single_port_client(tmp_path):
    """单端口生产模式：后端托管 dist，因此会注册 SPA 回退路由。

    **这个配置才是真机出问题的现场**：SPA 回退只声明了 GET，于是任何方法
    打到不存在的路径时，Starlette 会认为"路径匹配了但方法不允许"并返回
    405 `{"detail":"Method Not Allowed"}`，把排查方向误导到"方法用错了"，
    而真实原因是前端某处请求漏了 /api 前缀。
    若像默认夹具那样关掉前端托管，Starlette 会直接给 404，根本复现不出来。
    """
    base = load_settings()
    dist = base.project_root / "vue-frontend" / "dist"
    if not dist.is_dir():
        pytest.skip("vue-frontend/dist 不存在（尚未构建前端），跳过 SPA 回退相关断言")

    config = replace(
        base,
        data_dir=tmp_path / "data",
        server=replace(base.server, serve_frontend=True),      # 关键：打开 SPA 回退
        database=replace(base.database, url=f"sqlite:///{(tmp_path / 'single.db').as_posix()}"),
        storage=replace(
            base.storage,
            outputs_dir=str(tmp_path / "outputs"),
            uploads_dir=str(tmp_path / "uploads"),
        ),
        inference=replace(base.inference, preload=False),
        auth=replace(base.auth, jwt_secret="test-secret-key-for-pytest-only-0123456789"),
    )
    app = create_app(config)
    with TestClient(app) as test_client:
        yield test_client


def test_single_port_unknown_path_is_404_with_prefix_hint(single_port_client):
    """真机复现：漏了 /api 的 POST 必须报 404，并在文案里点名前缀问题。"""
    response = single_port_client.post("/detection/image")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "/api" in detail, detail


def test_single_port_wrong_method_is_405_not_404(single_port_client):
    """但真实存在、只是方法不对的路径仍必须是 405，不能被误报成 404。"""
    response = single_port_client.post("/api/tasks")
    assert response.status_code == 405
    assert "GET" in response.json()["detail"]


def test_single_port_spa_route_still_served(single_port_client):
    """SPA 前端路由不能被上面的判断影响，仍要返回首页。"""
    response = single_port_client.get("/detect")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


def test_single_port_root_serves_index(single_port_client):
    """`GET /` 必须返回首页 —— 这是树莓派上唯一能打开网页的方式。

    实测缺陷：`/ `会被 `/{full_path:path}` 以空串命中，而 `_within()` 拒空串，
    于是首页是 404 `{"detail":"页面不存在"}`。开发机走 Vite(`:3000`) 碰不到，
    单端口模式的盒子（Pi）则整个网页打不开。
    """
    response = single_port_client.get("/")
    assert response.status_code == 200, response.text[:200]
    assert "text/html" in response.headers.get("content-type", "")
    assert '<div id="app">' in response.text


# ------------------------------------------------- SPA 兜底的路径穿越（P0，已修）
# 背景：`spa_fallback` 曾直接 `dist / full_path`，而 full_path 整段来自 URL，
# 于是未授权的 `GET /%2e%2e/%2e%2e/config.yaml` 能读到仓库里的任意文件
# （实测返回 200 与真实内容，含 JWT 密钥与数据库）。下面把边界钉住。


@pytest.fixture()
def fake_dist(tmp_path):
    """一个自造的 dist：内含一个可服务的文件，外面放一个"绝不能读到"的诱饵。

    用自造目录而不是仓库里真实的 `vue-frontend/dist`，是为了让断言不依赖
    "前端是否构建过"，也避免测试去读仓库里的真实配置文件。
    """
    dist = tmp_path / "vue-frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>index</html>", encoding="utf-8")
    (dist / "app.js").write_text("console.log(1)", encoding="utf-8")
    # 诱饵放在 dist 之外（模拟仓库根的 config.yaml / backend/data/.jwt_secret）
    (tmp_path / "vue-frontend" / "secret-outside-dist.txt").write_text(
        "SHOULD-NEVER-BE-SERVED", encoding="utf-8"
    )
    return dist.resolve()


def test_within_rejects_parent_traversal(fake_dist):
    """`..` 逃逸必须被挡住（这就是当初那个 200）。"""
    assert _within(fake_dist, "../secret-outside-dist.txt") is None
    assert _within(fake_dist, "../../../config.yaml") is None
    assert _within(fake_dist, "assets/../../secret-outside-dist.txt") is None


def test_within_rejects_absolute_path_replacement(fake_dist):
    """绝对路径会把左操作数整个替换掉，连 `..` 都不用 —— Windows 上尤其危险。

    用当前平台的绝对路径构造，POSIX 与 Windows 都能覆盖到这条分支。
    """
    outside = Path.cwd() / "config.yaml"          # dist 之外的绝对路径
    assert outside.is_absolute()
    assert _within(fake_dist, str(outside)) is None


@pytest.mark.skipif(
    sys.platform != "win32", reason="反斜杠分隔符是 Windows 路径语义，Linux 上不是遍历"
)
def test_within_rejects_backslash_traversal(fake_dist):
    """Windows 的反斜杠也是分隔符：`..\\..` 与 `../..` 等价，必须一起挡。"""
    assert _within(fake_dist, "..\\secret-outside-dist.txt") is None


def test_within_rejects_symlink_pointing_outside(fake_dist, tmp_path):
    """dist 内的符号链接指向外面时也要挡住（resolve 之后再比前缀）。"""
    link = fake_dist / "sneaky.txt"
    target = tmp_path / "vue-frontend" / "secret-outside-dist.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接（Windows 非开发者模式）")
    if not link.is_symlink():
        # Windows 没开开发者模式时 `symlink_to` **不抛异常、也不真的建出链接**：
        # 调用返回正常，文件却不存在。此时下面那句断言是拿一个根本不存在的路径
        # 去测"必须挡住"，红的是环境而不是代码 —— 明确跳过，别让它伪装成产品缺陷。
        # （Linux / macOS / CI 上这一条照旧真跑。）
        pytest.skip("符号链接没有被真正创建（Windows 未开开发者模式）")
    assert _within(fake_dist, "sneaky.txt") is None


def test_within_allows_real_files_and_returns_none_for_missing(fake_dist):
    assert _within(fake_dist, "app.js") == fake_dist / "app.js"
    assert _within(fake_dist, "assets/../app.js") == fake_dist / "app.js"
    assert _within(fake_dist, "does-not-exist.js") == fake_dist / "does-not-exist.js"
    assert _within(fake_dist, "") is None


def test_single_port_traversal_is_404_not_a_leaked_file(single_port_client):
    """端到端：真机形态下穿越请求必须是 404，且响应体里没有文件内容。

    这里只断言状态码与"没漏内容"，不去读仓库里的敏感文件本体。
    """
    # 用百分号编码的 `..`：客户端的 URL 规范化不会动它，能一路走到路由参数里
    # （写成裸 `/../config.yaml` 会被 httpx 在发出前就规范化成 `/config.yaml`，
    #  那样测的就不是穿越而是普通 SPA 回退了）
    for path in ("/%2e%2e/%2e%2e/config.yaml", "/..%2f..%2fconfig.yaml"):
        response = single_port_client.get(path)
        assert response.status_code == 404, f"{path} 应被拒绝，实际 {response.status_code}"
        assert "SHOULD-NEVER-BE-SERVED" not in response.text
        # 更不能把任意文件的正文当页面吐出来（config.yaml 里有 paths / 端口等）
        assert "outputs_dir" not in response.text


def test_single_port_real_dist_file_is_still_served(single_port_client):
    """修穿越不能把正常的静态文件一起挡掉。"""
    response = single_port_client.get("/index.html")
    assert response.status_code == 200


@pytest.fixture()
def loaded_app_auth(app, client):
    """已登录的客户端 + 已加载模型的最小应用。"""
    weights = PROJECT_ROOT / "weights" / "best-yolo11s.pt"
    if not weights.is_file():
        # 权重被 .gitignore 排除（二进制不进仓库），CI 上没有它 —— 跳过而不是失败
        pytest.skip(f"缺少权重 {weights.name}（仓库不存二进制）")
    app.state.detector.load(str(weights))
    client.post("/api/auth/register", json={"username": "sec", "password": "secret123"})
    token = client.post(
        "/api/auth/login", json={"username": "sec", "password": "secret123"}
    ).json()["access_token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client
