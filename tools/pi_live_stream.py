#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""树莓派 CSI / USB 摄像头实时 MJPEG 推流（给主机浏览器看）。

为什么需要它：
  正式系统的「本机摄像头」在树莓派上取不到帧时会**静默回退到模拟图**
  （从 test_media 循环读图），于是主机界面看到的「训练照片」其实是兜底图。
  这个脚本是绕开整套后端的最小验证手段：

    1. 最快验证：在树莓派上跑它，Windows 主机浏览器打开
       http://<树莓派IP>:8080/ 立刻看到实时画面。
       - 能出图 => 摄像头硬件 + picamera2 都正常，问题只是正式系统的回退逻辑。
       - 出不了图 => 网页会直接打印确切原因（没装 / 没启用 / 排线没插好），
         不再靠猜。
    2. 临时看画面：不想折腾正式系统时的轻量替代。

依赖（树莓派 Pi OS）：
  CSI 摄像头：sudo apt install -y python3-picamera2
  USB 摄像头：系统通常自带 python3-opencv；没有就 pip install opencv-python-headless
  本脚本只用标准库 + 上面二者之一，不依赖整个项目。

用法：
  python3 pi_live_stream.py                  # 默认 0.0.0.0:8080
  python3 pi_live_stream.py --port 9000
  python3 pi_live_stream.py --width 960 --height 540 --fps 15
  python3 pi_live_stream.py --check          # 只诊断，不启动服务，打印完即退出
"""
from __future__ import annotations

import argparse
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

# camera 句柄与锁：推流连接可能多个，但摄像头同一时刻只能被一个进程/对象打开。
_CAMERA_LOCK = Lock()


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# --------------------------------------------------------------------- 诊断
def check_camera() -> list[str]:
    """探测本机摄像头可用性，返回可读的诊断行（不抛异常）。"""
    lines: list[str] = []

    # 1) picamera2（CSI）
    try:
        from picamera2 import Picamera2  # noqa: F401
    except Exception as exc:  # 没装 / 装坏
        lines.append(f"[CSI/picamera2] 未安装：{type(exc).__name__}: {exc}")
        lines.append("    修复：sudo apt update && sudo apt install -y python3-picamera2")
    else:
        try:
            found = list(Picamera2.global_camera_info())
            if not found:
                lines.append("[CSI/picamera2] 已安装但未检测到摄像头")
                lines.append("    修复：检查排线是否插紧；Bookworm 默认启用，")
                lines.append("          旧系统用 sudo raspi-config → Interface → Camera 开启后重启")
                lines.append("    验证：libcamera-hello --timeout 2000  （应短暂出现预览）")
            else:
                names = ", ".join(str(i.get("Model") or "CSI") for i in found)
                lines.append(f"[CSI/picamera2] 正常：检测到 {len(found)} 个摄像头：{names}")
        except Exception as exc:
            lines.append(f"[CSI/picamera2] 已安装但枚举失败：{type(exc).__name__}: {exc}")

    # 2) opencv（USB / V4L2）
    try:
        import cv2  # noqa: F401
    except Exception as exc:
        lines.append(f"[USB/opencv] 未安装：{type(exc).__name__}: {exc}")
        lines.append("    修复：pip install opencv-python-headless")
    else:
        import os

        nodes = sorted(os.listdir("/dev")) if os.path.exists("/dev") else []
        video_nodes = [n for n in nodes if n.startswith("video")]
        if video_nodes:
            lines.append(f"[USB/opencv] 可用：发现 V4L2 节点 {', '.join(video_nodes)}")
        else:
            lines.append("[USB/opencv] 未发现 /dev/video* 节点（可能没有 USB 摄像头）")
    return lines


# --------------------------------------------------------------------- 图像源
class Picamera2Source:
    name = "picamera2"

    def __init__(self, width: int, height: int) -> None:
        self._w, self._h = width, height
        self._cam = None

    def open(self) -> str:
        from picamera2 import Picamera2

        cam = Picamera2()
        cam.configure(
            cam.create_video_configuration(
                main={"size": (self._w, self._h), "format": "RGB888"}
            )
        )
        cam.start()
        time.sleep(1.0)  # 等 AGC/AWB 收敛，否则首帧偏色
        self._cam = cam
        return "picamera2"

    def grab(self):
        import numpy as np

        arr = self._cam.capture_array()
        if arr is None:
            return None
        # picamera2 给的是 RGB，cv2 要 BGR
        return np.ascontiguousarray(arr[:, :, ::-1])

    def close(self) -> None:
        if self._cam is not None:
            try:
                self._cam.stop()
                self._cam.close()
            except Exception:
                pass
            self._cam = None


class OpenCvSource:
    name = "opencv"

    def __init__(self, device: str, width: int, height: int) -> None:
        self._device = device
        self._w, self._h = width, height
        self._cap = None

    def open(self) -> str:
        import cv2

        target = int(self._device) if str(self._device).isdigit() else self._device
        cap = cv2.VideoCapture(target)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"无法打开摄像头设备：{self._device}")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._h)
        for _ in range(5):  # 丢前几帧，等曝光稳定
            cap.read()
        self._cap = cap
        return "opencv"

    def grab(self):
        ok, frame = self._cap.read()
        return frame if ok else None

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


def build_source(width: int, height: int) -> tuple[object, str]:
    """按优先级尝试打开图像源：picamera2(CSI) > opencv(USB)。返回 (源, 后端名)。"""
    failures: list[str] = []
    for cls, kwargs in (
        (Picamera2Source, {"width": width, "height": height}),
        (OpenCvSource, {"device": "0", "width": width, "height": height}),
    ):
        try:
            src = cls(**kwargs)
            backend = src.open()
            if src.grab() is None:
                raise RuntimeError("能打开但读不出帧（设备被占用或节点不可读？）")
            return src, backend
        except Exception as exc:  # 换下一个继续试
            try:
                src.close()
            except Exception:
                pass
            failures.append(f"{cls.name}（{type(exc).__name__}: {exc}）")
    raise RuntimeError("；".join(failures) or "没有可用的图像源")


# --------------------------------------------------------------------- HTTP
class _StreamState:
    """在 handler 之间共享的摄像头状态。"""

    def __init__(self) -> None:
        self.source = None
        self.backend = "（未初始化）"
        self.error = "正在初始化摄像头…"
        self.last_frame = None  # 最近一帧 JPEG，供首屏和失败兜底展示


_STATE = _StreamState()


def _index_page() -> bytes:
    backend = _html_escape(_STATE.backend)
    err = _html_escape(_STATE.error)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>树莓派实时画面</title>
<style>
 body{{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:0;background:#0f1115;color:#e6e6e6}}
 header{{padding:10px 16px;background:#1b1f27;display:flex;gap:12px;align-items:center}}
 header b{{font-size:15px}}
 .tag{{font-size:12px;padding:2px 8px;border-radius:10px;background:#2a3140}}
 .tag.ok{{background:#1f7a3d}} .tag.bad{{background:#7a2f2f}}
 main{{padding:16px;display:flex;flex-direction:column;gap:12px;align-items:center}}
 img{{max-width:100%;border-radius:8px;background:#000;min-height:240px}}
 .err{{max-width:760px;white-space:pre-wrap;background:#241616;color:#ffb4b4;
       padding:12px 14px;border-radius:8px;font-size:13px;line-height:1.6}}
 .hint{{color:#9aa4b2;font-size:13px}}
</style></head>
<body>
<header><b>树莓派实时画面</b><span class="tag {('ok' if _STATE.error=='OK' else 'bad')}">{backend}</span></header>
<main>
 <img id="v" src="/stream.mjpg" alt="实时画面">
 <div class="hint">主机浏览器打开本页面即为实时画面；若长时间黑屏或显示错误，见下方说明。</div>
 <div class="err" id="err"></div>
</main>
<script>
 // 流断开时轮询一次状态，把确切错误显示出来（避免干瞪黑屏）
 function refreshError(){{ fetch('/status').then(r=>r.json()).then(d=>{{
   const el=document.getElementById('err');
   if(d.error && d.error!=='OK'){{ el.textContent='状态：'+d.error; }}
 }}).catch(()=>{{}}); }}
 setInterval(refreshError, 3000);
 document.getElementById('v').onerror=refreshError;
</script>
</body></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # 必须，否则 keep-alive 的 MJPEG 流会出问题

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, _index_page(), "text/html; charset=utf-8")
        elif self.path == "/stream.mjpg":
            self._stream()
        elif self.path == "/status":
            import json

            body = json.dumps(
                {"backend": _STATE.backend, "error": _STATE.error}
            ).encode("utf-8")
            self._send(200, body, "application/json")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def _stream(self) -> None:
        boundary = b"ipframe"
        self.send_response(200)
        self.send_header(
            "Content-Type", f"multipart/x-mixed-replace; boundary={boundary.decode()}"
        )
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                with _CAMERA_LOCK:
                    src = _STATE.source
                    if src is None:
                        break
                    frame = src.grab()
                if frame is None:
                    time.sleep(0.1)
                    continue
                import cv2

                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if not ok:
                    continue
                jpeg = buf.tobytes()
                self.wfile.write(b"--" + boundary + b"\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
                time.sleep(1.0 / max(1, _ARGS.fps))
        except (BrokenPipeError, ConnectionResetError):
            pass  # 浏览器关掉页面，正常

    def log_message(self, *args) -> None:  # 关掉每帧的访问日志，避免刷屏
        pass


def _init_camera(width: int, height: int) -> None:
    try:
        src, backend = build_source(width, height)
        _STATE.source = src
        _STATE.backend = backend
        _STATE.error = "OK"
        print(f"[OK] 摄像头已打开：{backend}  ({width}x{height})")
    except Exception as exc:
        _STATE.error = f"{type(exc).__name__}: {exc}"
        print(f"[FAIL] 摄像头初始化失败：{exc}")


# --------------------------------------------------------------------- 入口
_ARGS: argparse.Namespace


def main() -> None:
    global _ARGS
    parser = argparse.ArgumentParser(description="树莓派摄像头实时 MJPEG 推流")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument(
        "--check", action="store_true", help="只打印摄像头诊断信息，不启动服务"
    )
    _ARGS = parser.parse_args()

    if _ARGS.check:
        print("==== 摄像头诊断 ====")
        for line in check_camera():
            print(line)
        return

    _init_camera(_ARGS.width, _ARGS.height)
    server = ThreadingHTTPServer((_ARGS.host, _ARGS.port), Handler)
    print(f"[INFO] 推流已启动：http://{_ARGS.host}:{_ARGS.port}/")
    print(f"[INFO] 在 Windows 主机浏览器打开 http://<树莓派IP>:{_ARGS.port}/ 查看实时画面")
    if _ARGS.error != "OK":
        print("[WARN] 摄像头未就绪，网页会显示错误原因；先修复上面的问题。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] 已停止")


if __name__ == "__main__":
    main()
