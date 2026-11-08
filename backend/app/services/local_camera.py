# -*- coding: utf-8 -*-
"""边缘盒子（树莓派）本机直连摄像头的采集服务。

**为什么需要它**：给盒子直接插一个摄像头，不需要额外硬件；但后端必须自己能取图，
否则树莓派手里有摄像头也用不上（此前正是这个缺口）。
这是本项目唯一的自动采集路径（原先还支持远端采集节点拍照上传，已随设备层移除）。

三种图像源：
  * `picamera2`  —— 树莓派 CSI 排线摄像头（Pi Camera Module），Pi OS 官方栈
  * `opencv`     —— USB 摄像头 / V4L2 设备
  * `simulated`  —— 从目录循环读图。**不是玩具**：它让整条链路（定时器 →
                    推理 → 落库 → 历史/看板/报告）在没有摄像头的机器上也能
                    被自动化测试和人工验证，否则这个功能只能等硬件到货才敢说"能用"。

取图与推理都在**后台线程**里做：picamera2 打开设备要 1~2 秒、
推理又是重活，放到事件循环里会把整个后端卡住。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

import cv2
import numpy as np

from ..config import LocalCameraConfig, Settings
from ..models import Task, TaskMode, TaskStatus, utcnow
from .capture import persist_frame_task
from .detector import DetectorService
from .hostinfo import describe_host

logger = logging.getLogger("insect.localcam")

# 一分钟的秒数。抽成模块级常量是为了让测试能把它改小，
# 从而**真实验证定时抓拍这条路径**（否则只能等满一分钟，或者干脆不测）。
SECONDS_PER_MINUTE = 60


# ---------------------------------------------------------------- 探测
def _probe_picamera2() -> tuple[bool, str]:
    """探测 CSI 排线摄像头是不是**真的在**，返回 `(可用, 说明)`。

    **只看 `import picamera2` 是不够的**，这一点踩过：Pi OS 预装了这个包，
    于是"包能导入"和"盒子上真的插着排线摄像头"完全是两件事。之前 auto 只判
    前者，结果在没有 CSI 摄像头的盒子（比如只插了 USB 摄像头）上照样选
    picamera2，然后**每一帧**都在 `Picamera2()` 构造处抛错，推流永远黑着，
    而且没有任何回退 —— 从网页上看只是"盒子在线但没画面"，看不出原因。

    `global_camera_info()` 只枚举设备、不打开它，因此既安全又快，可以放心
    在每次状态查询时调用。
    """
    try:
        from picamera2 import Picamera2
    except Exception as exc:  # noqa: BLE001 - 没装、装坏、缺 libcamera 都归到这里
        return False, f"未安装（{type(exc).__name__}）；树莓派上执行 sudo apt install -y python3-picamera2"

    try:
        probe = getattr(Picamera2, "global_camera_info", None)
        if callable(probe):
            found = list(probe())
        else:  # pragma: no cover - 老版本没有这个接口
            camera = Picamera2()
            try:
                camera.close()
            except Exception:  # noqa: BLE001 - 探测成功就够了
                pass
            found = [{"Model": "unknown"}]
    except Exception as exc:  # noqa: BLE001 - 枚举失败等同"没有摄像头"
        return False, f"已安装但枚举摄像头失败（{type(exc).__name__}: {exc}）"

    if not found:
        return False, "已安装但未检测到 CSI 摄像头（排线没插好？或 raspi-config 里未启用？）"
    names = ", ".join(str(item.get("Model") or "CSI") for item in found)
    return True, f"检测到 {len(found)} 个 CSI 摄像头：{names}"


# ---------------------------------------------------------------- 图像源
class CameraSource(Protocol):
    name: str

    def open(self) -> None: ...
    def grab(self) -> np.ndarray | None: ...
    def close(self) -> None: ...
    def info(self) -> dict[str, Any]: ...


def _decode_file(path: Path) -> np.ndarray | None:
    """中文路径安全读图（cv2.imread 在含中文的路径上会静默失败）。"""
    try:
        buffer = np.fromfile(str(path), dtype=np.uint8)
        return cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    except Exception:  # noqa: BLE001
        return None


class Picamera2Source:
    """树莓派 CSI 摄像头。需要 `python3-picamera2`（Pi OS 自带 libcamera 栈）。"""

    name = "picamera2"

    def __init__(self, width: int, height: int, warmup_frames: int) -> None:
        self._width = width
        self._height = height
        self._warmup = max(0, warmup_frames)
        self._camera: Any = None

    def open(self) -> None:
        from picamera2 import Picamera2  # 惰性导入：非树莓派环境不该因它 import 失败

        camera = Picamera2()
        # RGB888 是 picamera2 的原生输出；转 BGR 后与 cv2 全链路一致
        camera.configure(
            camera.create_still_configuration(
                main={"size": (self._width, self._height), "format": "RGB888"}
            )
        )
        camera.start()
        time.sleep(1.0)  # 等 AGC/AWB 收敛，否则首帧常偏色
        self._camera = camera

    def grab(self) -> np.ndarray | None:
        if self._camera is None:
            return None
        array = self._camera.capture_array()
        if array is None:
            return None
        return np.ascontiguousarray(array[:, :, ::-1])  # RGB -> BGR

    def close(self) -> None:
        if self._camera is not None:
            try:
                self._camera.stop()
                self._camera.close()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不应影响停止流程
                logger.warning("关闭 picamera2 失败：%s", exc)
            self._camera = None

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "width": self._width,
            "height": self._height,
            "requires": "python3-picamera2",
        }


class OpenCvSource:
    """USB 摄像头 / V4L2 设备。`device` 可为索引（"0"）或路径（"/dev/video0"）。"""

    name = "opencv"

    def __init__(self, device: str, width: int, height: int, warmup_frames: int) -> None:
        self._device = device
        self._width = width
        self._height = height
        self._warmup = max(0, warmup_frames)
        self._capture: cv2.VideoCapture | None = None

    def _target(self) -> Any:
        text = str(self._device).strip()
        return int(text) if text.isdigit() else text

    def open(self) -> None:
        capture = cv2.VideoCapture(self._target())
        if not capture.isOpened():
            capture.release()
            raise RuntimeError(f"无法打开摄像头设备：{self._device}")
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
        # 丢掉前几帧：自动曝光/白平衡需要时间稳定，首帧常是全黑或偏色
        for _ in range(self._warmup):
            capture.read()
        self._capture = capture

    def grab(self) -> np.ndarray | None:
        if self._capture is None:
            return None
        ok, frame = self._capture.read()
        return frame if ok else None

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "device": self._device,
            "width": self._width,
            "height": self._height,
        }


class SimulatedSource:
    """从目录循环读图，作为无硬件环境下的图像源。

    用途有两个，都很实际：
      1. 开发机/CI 上验证整条链路（定时抓拍 → 推理 → 落库 → 历史/看板/报告）
      2. 现场演示：没有摄像头时也能让系统跑起来给人看
    因此它**不是**只在测试里存在的桩，而是配置里可选的一等后端。
    """

    name = "simulated"

    def __init__(self, directory: Path, warmup_frames: int = 0) -> None:
        self._dir = directory
        self._warmup = max(0, warmup_frames)
        self._files: list[Path] = []
        self._cursor = 0

    def open(self) -> None:
        if not self._dir.is_dir():
            raise RuntimeError(f"模拟图像目录不存在：{self._dir}")
        files: list[Path] = []
        for pattern in ("*.jpg", "*.jpeg", "*.png", "*.bmp"):
            files.extend(sorted(self._dir.glob(pattern)))
        if not files:
            raise RuntimeError(f"模拟图像目录里没有图片：{self._dir}")
        self._files = files
        self._cursor = 0

    def grab(self) -> np.ndarray | None:
        if not self._files:
            return None
        # 轮转取图：每次抓拍换一张，这样连续几次抓拍能得到不同结果，
        # 否则任务列表里会出现一串完全相同的记录，看不出链路是否真的在工作。
        path = self._files[self._cursor % len(self._files)]
        self._cursor += 1
        frame = _decode_file(path)
        if frame is None:
            logger.warning("模拟图像解码失败：%s", path)
        return frame

    def close(self) -> None:
        self._files = []
        self._cursor = 0

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "directory": str(self._dir),
            "image_count": len(self._files),
            "note": "无硬件环境下的模拟图像源（开发/演示/自动化测试）",
        }


# ---------------------------------------------------------------- 服务
class LocalCameraService:
    """本机摄像头的定时抓拍与状态管理。"""

    def __init__(
        self,
        settings: Settings,
        session_factory: Callable[[], Any],
        detector: DetectorService,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._detector = detector
        self._config: LocalCameraConfig = settings.local_camera

        self._lock = threading.RLock()
        self._source: CameraSource | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._capture_count = 0
        self._last_capture_at = None
        self._last_task_id: int | None = None
        self._last_error: str | None = None
        self._last_error_at = None
        #: auto 回退到非首选图像源时的常驻说明（见 `_ensure_open`）。
        #: 与 `_last_error` 分开存，是因为抓拍成功会清掉 `_last_error`，
        #: 而"画面其实来自 simulated"这件事在成功时也仍然要说清楚。
        self._fallback_note: str | None = None
        self._next_capture_at: float | None = None

    # ---------------------------------------------------------------- 探测
    def _simulated_dir(self) -> Path:
        raw = Path(self._config.simulated_dir)
        return raw if raw.is_absolute() else (self._settings.project_root / raw).resolve()

    def available_backends(self) -> list[dict[str, Any]]:
        """探测本机可用的图像源，供页面展示与排障。"""
        result: list[dict[str, Any]] = []

        ok, detail = _probe_picamera2()
        result.append({"backend": "picamera2", "available": ok, "detail": detail})

        video_nodes = sorted(Path("/dev").glob("video*")) if Path("/dev").exists() else []
        result.append(
            {
                "backend": "opencv",
                "available": True,  # cv2 装了就可能用；具体设备能否打开要实际试
                "detail": "V4L2 设备：" + (", ".join(p.name for p in video_nodes) or "未发现 /dev/video*"),
            }
        )

        sim_dir = self._simulated_dir()
        sim_count = 0
        if sim_dir.is_dir():
            for pattern in ("*.jpg", "*.jpeg", "*.png", "*.bmp"):
                sim_count += len(list(sim_dir.glob(pattern)))
        result.append(
            {
                "backend": "simulated",
                "available": sim_count > 0,
                "detail": f"{sim_dir}（{sim_count} 张图片）",
            }
        )
        return result

    def candidate_backends(self) -> list[str]:
        """auto 解析出的**候选链**，按优先级排列；逐个试到能出图为止。

        为什么是"链"而不是一个值：摄像头这件事在盒子上经常是"看起来能打开、
        实际上读不出帧"（OpenCV 打 libcamera 建的 V4L2 节点就是这样），
        而排线摄像头又可能在开机后才被发现。只定一个后端，一旦它打不开就
        再也没有别的路 —— 推流会一直黑着。链上每个候选都必须**真读出一帧**
        才算数（`_ensure_open` 里判），打不开的和读不出帧的都会被跳过。

        auto 的优先级：picamera2（有 CSI）> opencv（有 V4L2 节点）> simulated。
        simulated 兜底是为了"没有摄像头也能先跑通整条链路"（开发/演示/测试），
        代价是真实摄像头坏掉时会拿测试图顶上 —— 因此回退原因一律记进
        `last_error`，页面上能一眼看出画面不是真的。
        """
        wanted = (self._config.backend or "auto").strip().lower()
        if wanted != "auto":
            # 显式指定时不自作主张换后端：用户选了什么就该拿到什么，
            # 错了就把错误原样报出来（"沉默地换一个"比报错更难查）。
            return [wanted]

        order: list[str] = []
        if _probe_picamera2()[0]:
            order.append("picamera2")
        # opencv 的"可用"只代表 cv2 能导入，设备能否打开要开了才知道；
        # 但相对 simulated 它更接近真实部署，所以优先。
        if not Path("/dev").exists() or any(Path("/dev").glob("video*")):
            order.append("opencv")
        order.append("simulated")
        return order

    def resolve_backend(self) -> str:
        """候选链的第一个（即 auto 下最优先的那个）。

        状态页用它回答"现在打算用谁"；真正用的是 `_ensure_open` 挑定的那一个。
        """
        return self.candidate_backends()[0]

    # ---------------------------------------------------------------- 图像源
    def _build_source(self, backend: str) -> CameraSource:
        if backend == "picamera2":
            return Picamera2Source(self._config.width, self._config.height, self._config.warmup_frames)
        if backend == "opencv":
            return OpenCvSource(
                self._config.device, self._config.width, self._config.height, self._config.warmup_frames
            )
        if backend == "simulated":
            return SimulatedSource(self._simulated_dir(), self._config.warmup_frames)
        raise RuntimeError(f"未知的摄像头后端：{backend}")

    def _ensure_open(self) -> CameraSource:
        """惰性打开并复用：picamera2 打开要 1~2 秒，每次都开代价太大。

        打开**按候选链逐个试**，并且每个候选都必须**真的读出一帧**才收下 ——
        "能打开但读不出帧"在 V4L2 节点上很常见，只判 `isOpened()` 会留下一个
        永远出不了图的假可用源。全部失败时把所有候选的失败原因拼在一起抛出，
        顺便记进 `last_error`：现场最需要的是"为什么没画面"，而不是一句
        "取帧失败"。

        注意调用方必须已持有 self._lock —— 定时线程与页面手动触发可能同时到达。
        """
        if self._source is not None:
            return self._source

        failures: list[str] = []
        for backend in self.candidate_backends():
            source = self._build_source(backend)
            try:
                source.open()
                if source.grab() is None:
                    raise RuntimeError("能打开但读不出帧（设备被占用或节点不可读？）")
            except Exception as exc:  # noqa: BLE001 - 换下一个候选继续试
                self._safe_close(source)
                failures.append(f"{backend}（{type(exc).__name__}: {exc}）")
                continue
            self._source = source
            if failures:
                reason = "；".join(failures)
                # 记成一份**常驻**说明而不是一次性的 last_error：回退到 simulated 时
                # 每一拍都会"成功"，若按成功清掉它，页面上就再也看不出
                # 画面其实来自 test_media、不是田里的摄像头。
                self._fallback_note = f"已跳过不可用的图像源，当前画面来自 {backend}：{reason}"
                self._last_error = self._fallback_note
                logger.warning(
                    "本机摄像头回退到 %s（以下图像源不可用：%s）", backend, reason
                )
            else:
                self._fallback_note = None
                self._last_error = None
            logger.info("本机摄像头已打开：%s", source.info())
            return source

        reason = "；".join(failures) or "配置里没有可用的图像源"
        self._last_error = reason
        raise RuntimeError(f"没有可用的图像源：{reason}")

    @staticmethod
    def _safe_close(source: CameraSource) -> None:
        """关掉一个试了一半的图像源。失败无所谓 —— 它本来就是被放弃的那个。"""
        try:
            source.close()
        except Exception:  # noqa: BLE001
            pass

    def close_camera(self) -> None:
        with self._lock:
            if self._source is not None:
                self._source.close()
                self._source = None

    def grab_frame(self) -> tuple[np.ndarray, dict[str, Any]]:
        """取一帧，返回 `(BGR 图, 图像源信息)`。线程安全。

        抽出来是为了让**推流**与**定时推理**共用同一个 `CameraSource`。
        这两件事节奏差得很远（2fps 取帧 vs 每 30 分钟推理一次），
        但设备只有一个：CSI/USB 摄像头同一时刻只能被一个进程打开，
        而在进程内再开第二个 `CameraSource` 等于抢设备。所以取帧这件事
        必须收口到一处，由 `self._lock`（RLock）串行化。

        取帧失败时**关闭摄像头**再抛：失败通常是设备被占用或已被拔掉，
        关掉之后下一次调用会重新打开，比抱着一个坏句柄一直失败强。

        异常而非返回 None：调用方（定时抓拍、推流循环）对失败的处理不同，
        而"没有帧"在两边都是必须停下来的条件，用异常更难被忽略。
        """
        with self._lock:
            source = self._ensure_open()
            frame = source.grab()
            if frame is None:
                self.close_camera()
                raise RuntimeError("取帧失败（摄像头被占用或已断开？）")
            return frame, source.info()

    # ---------------------------------------------------------------- 抓拍
    def capture_once(self, *, trigger: str = "manual") -> dict[str, Any]:
        """抓一拍 → 推理 → 落库。同步执行，调用方负责放到线程里跑。

        返回一条结果摘要；失败时 catch 由调用方决定（定时线程里只记日志不抛出）。
        """
        started = time.perf_counter()
        frame, source_info = self.grab_frame()
        # 直接取键而不是 `.get(...) or resolve_backend()`：三个图像源的
        # info() 都带 name，真缺了就该在出错的那一行炸出来，
        # 而不是悄悄换成一个"可能是另一个后端"的字符串。
        backend = str(source_info["name"])

        db = self._session_factory()
        try:
            task = Task(
                mode=TaskMode.LOCAL,
                status=TaskStatus.RUNNING,
                source_name=f"本机摄像头（{backend}）",
                conf=self._config.conf or self._settings.inference.conf,
                iou=self._config.iou or self._settings.inference.iou,
                total_frames=1,
            )
            db.add(task)
            db.commit()
            db.refresh(task)

            result = self._detector.infer(frame, task.conf, task.iou)
            counts = persist_frame_task(
                db, self._settings, task, frame, result.detections, result.inference_ms
            )
            from .alerts import evaluate_task  # 局部导入避免模块级循环依赖

            triggered = evaluate_task(db, task)

            elapsed = (time.perf_counter() - started) * 1000
            with self._lock:
                self._capture_count += 1
                self._last_capture_at = utcnow()
                self._last_task_id = task.id
                # 清掉上一次的失败，但**保留回退说明**：跑在 simulated 上时
                # 每一拍都是"成功"的，清掉它等于告诉用户画面来自真实摄像头。
                self._last_error = self._fallback_note
            logger.info(
                "本机抓拍完成（%s）：task=%s 检出=%d 耗时=%.0fms",
                trigger,
                task.id,
                len(result.detections),
                elapsed,
            )
            return {
                "task_id": task.id,
                "trigger": trigger,
                "backend": backend,
                "source": source_info,
                "total_objects": task.total_objects,
                "unique_objects": task.unique_objects,
                "counts": counts,
                "inference_ms": round(result.inference_ms, 2),
                "alerts": [a.message for a in triggered],
            }
        finally:
            db.close()

    def _record_error(self, exc: Exception) -> None:
        with self._lock:
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._last_error_at = utcnow()
        logger.warning("本机抓拍失败：%s", exc)

    # ---------------------------------------------------------------- 定时
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> dict[str, Any]:
        """启动定时抓拍。间隔为 0 时不启动线程（只允许手动触发）。"""
        with self._lock:
            if self.running:
                return self.status()
            interval = int(self._config.interval_min)
            if interval <= 0:
                return self.status()
            self._stop_event.clear()
            self._wake_event.clear()
            self._thread = threading.Thread(target=self._loop, name="local-camera", daemon=True)
            self._thread.start()
            logger.info("本机摄像头定时抓拍已启动，间隔 %d 分钟", interval)
        return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop_event.set()
            self._wake_event.set()
            thread = self._thread
            self._thread = None
            self._next_capture_at = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)
        # 一并释放摄像头：USB 摄像头的指示灯会灭掉，设备也让给别的程序用。
        # 之前只停线程不关设备，与接口文档写的"停止并释放"不符
        # （实测 stop 之后 camera_open 仍为 true）。
        self.close_camera()
        logger.info("本机摄像头定时抓拍已停止")
        return self.status()

    def _loop(self) -> None:
        interval_s = max(1, int(self._config.interval_min) * SECONDS_PER_MINUTE)
        while not self._stop_event.is_set():
            # 用 Event.wait 而不是 time.sleep：停止时能立刻醒来，不必等满一个间隔
            self._next_capture_at = time.time() + interval_s
            if self._wake_event.wait(interval_s):
                self._wake_event.clear()
                continue
            if self._stop_event.is_set():
                break
            # 相机预热也要算进实际等待里，所以这里只记"大概"的下次时间
            try:
                self.capture_once(trigger="schedule")
            except Exception as exc:  # noqa: BLE001 - 定时线程绝不能因单次失败而退出
                self._record_error(exc)
        self._next_capture_at = None

    def shutdown(self) -> None:
        self.stop()
        self.close_camera()

    # ---------------------------------------------------------------- 状态
    def status(self) -> dict[str, Any]:
        with self._lock:
            next_in: int | None = None
            if self._next_capture_at is not None and self.running:
                next_in = max(0, int(self._next_capture_at - time.time()))
            return {
                "enabled": bool(self._config.enabled),
                "running": self.running,
                "backend": self._source.name if self._source is not None else self.resolve_backend(),
                "configured_backend": self._config.backend,
                "device": self._config.device,
                "interval_min": int(self._config.interval_min),
                "camera_open": self._source is not None,
                "capture_count": self._capture_count,
                "last_capture_at": self._last_capture_at,
                "last_task_id": self._last_task_id,
                "last_error": self._last_error,
                "last_error_at": self._last_error_at,
                "next_capture_in_s": next_in,
                "available": self.available_backends(),
                # 让前端能说明"这个面板控制的是哪台设备"（现场＝树莓派）
                "host": describe_host(),
            }
