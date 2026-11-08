/**
 * 全局类型定义 —— 严格对应后端已冻结的 API 契约。
 * 注意：项目开启了 noUncheckedIndexedAccess，数组下标访问会得到 T | undefined。
 */

/* ------------------------------- 鉴权 ------------------------------- */

export interface UserOut {
  id: number
  username: string
  avatar_path: string | null
  must_reset_password: boolean
  created_at: string
  last_login_at: string | null
}

export interface LoginPayload {
  username: string
  password: string
}

export interface LoginResponse {
  access_token: string
  token_type: string
  expires_in: number
  user: UserOut
}

export interface MessageResponse {
  message: string
}

export interface AvatarResponse {
  avatar_path: string
}

/* ------------------------------- 系统 ------------------------------- */

export interface HealthOut {
  status: string
  version: string
  device: string
  gpu_available: boolean
  backend: string
  model_loaded: boolean
  model_path: string | null
  /**
   * **类别数**（数字），不是类别名数组。
   * 后端 HealthOut 里是 `model_names: int | None`；前端曾误声明为 string[]，
   * 于是 `model_names.length` 恒为 undefined —— 页头永远显示"类别 0 个"、
   * 设置页明明模型已加载却提示"未返回类别列表"。
   */
  model_names: number | null
  /**
   * 后端回退原因。非空说明配置的推理后端没起来、实际回退了 torch。
   * 部署到边缘设备时，这是判断"ONNX 是否真的生效"的关键字段。
   */
  fallback_reason?: string | null
}

/* ---------------------------- 主机状态（设备状态页） ---------------------------- */

/**
 * 运行后端这台主机（现场＝树莓派）的健康指标。
 *
 * **`null` 表示「没测到」，不是 0** —— 这是后端契约里最要紧的一条
 * （口径来自 deploy/field-soak.sh：空值代表没测到）。温度/降频/重启次数
 * 只在树莓派上拿得到，开发机上全是 null，界面必须显示「未测到」，
 * 绝不能把 null 渲染成 0，否则会把一个测不到指标的盒子误判为健康。
 */
export interface HostStatusOut {
  /* 设备身份 */
  platform: string
  machine: string
  model: string | null
  is_raspberry_pi: boolean

  /** CPU 温度（℃）；null = 没有 vcgencmd */
  cpu_temp_c: number | null
  /** get_throttled 原始值，如 "0x0"（与 field-soak 的 throttled 列逐字一致） */
  throttled: string | null
  /** 派生：true=正常(0x0)，false=有欠压/降频，null=没测到 */
  throttled_ok: boolean | null
  /** 位掩码解码出的中文标志（欠压 / 正在降频 / 曾欠压 …） */
  throttled_flags: string[]

  disk_free_mb: number | null
  disk_total_mb: number | null
  disk_used_percent: number | null
  /** 用 MemAvailable 而非 MemFree —— 后者在正常运行的机器上常年很小，易误读 */
  mem_free_mb: number | null
  mem_total_mb: number | null

  /** 本进程已运行秒数（任何平台都有） */
  uptime_s: number | null
  /** systemd 的 NRestarts；非 systemd 部署下为 null */
  restarts: number | null

  sampled_at: string | null
}

/* ---------------------------- 边缘代理（树莓派 → 中心） ---------------------------- */

/**
 * 运行后端的这台机器上的边缘代理状态（`GET /api/system/agent`）。
 *
 * 只有配置成盒子的机器（`agent.enabled=true`）才有意义；开发机/中心机上
 * `enabled=false` 不是错误，设置页据此把「实时识别」整块隐藏。
 */
export interface AgentStatusOut {
  enabled: boolean
  running: boolean
  center_url: string
  /** 稳定硬件标识（Pi 的 Serial）；重新配对靠它认出"还是同一台" */
  hardware_id: string
  name: string
  device_id: number | null
  /** 有令牌即为已配对（后端派生，不落库） */
  paired: boolean
  /** 推流 WebSocket 当前活着（后端派生，不落库） */
  streaming: boolean
  /** 配置里是否允许推流（`agent.stream_enabled`） */
  stream_enabled: boolean
  /** 是否在推流画面上本机推理并画框（落盘偏好，默认 false） */
  live_detection: boolean
  /** 还没绑上时才有值；绑上后为空串 */
  pairing_code: string
  capture_interval_min: number
  seq: number
  frames_sent: number
  skipped_frames: number
  captures: number
  uploads: number
  pending_uploads: number
  /** `null` = 还没上报过，不是失败 */
  last_upload_at: string | null
  /** 最近一次错误（中文）；`null` = 没出过错 */
  last_error: string | null
  last_error_at: string | null
  uptime_s: number | null
}

/* ---------------------------- 本机摄像头（树莓派直连） ---------------------------- */

export interface LocalCameraBackendOut {
  backend: string
  available: boolean
  detail: string
}

export interface LocalCameraStatusOut {
  enabled: boolean
  running: boolean
  /** 实际使用的后端（auto 已被后端解析成具体值） */
  backend: string
  configured_backend: string
  device: string
  interval_min: number
  camera_open: boolean
  capture_count: number
  last_capture_at: string | null
  last_task_id: number | null
  last_error: string | null
  last_error_at: string | null
  next_capture_in_s: number | null
  available: LocalCameraBackendOut[]
  /** 后端运行所在的设备（现场＝树莓派）；面板据此说明"本机"到底是哪一台 */
  host: {
    platform: string
    machine: string
    model: string | null
    is_raspberry_pi: boolean
  } | null
}

export interface LocalCameraCaptureOut {
  task_id: number
  trigger: string
  backend: string
  total_objects: number
  unique_objects: number
  counts: Record<string, number>
  inference_ms: number
  alerts: string[]
}

/* ------------------------- 我的设备（绑定的树莓派） ------------------------- */

/**
 * 一台已绑定设备对网页的呈现。
 *
 * 与「设备状态」页（`HostStatusOut`）的区别：那一页讲的是**跑后端这台机器**，
 * 这里讲的是**挂在账号下的田里的盒子**。两者在单机部署时是同一台 Pi，
 * 但概念不同，字段也不同。
 *
 * 两个 `null` 的约定与 `HostStatusOut` 一致：**没测到 / 还没发生**，不是 0。
 * `last_task_at` 为 null 是「还没上报过」，不是「上报失败」。
 */
export interface DeviceOut {
  id: number
  /** 稳定硬件标识（Pi 的 /proc/cpuinfo Serial）。重新配对靠它认出是同一台盒子 */
  hardware_id: string
  name: string
  model: string | null
  /** 生命周期状态：pending / active / unbound。**与 `online` 是两回事** */
  status: string
  plot_id: number | null
  agent_version: string | null
  backend: string | null
  last_seen_at: string | null
  last_frame_at: string | null
  last_task_at: string | null
  created_at: string | null
  bound_at: string | null
  /**
   * 存活状态，**后端派生、不落库**（最近 60 秒内有心跳，且内存里确实有它的帧）。
   * 前端**不要**再自己按 `last_seen_at` 算一遍：两份判定会在最需要准确的时刻打架
   * （后端因为 hub 里没有帧判离线，前端却因为 30 秒前有心跳判在线）。
   */
  online: boolean
  /** 最近实测的推流帧率；0 表示没在推流 */
  stream_fps: number

  /* ---- 盒子自报的诊断信息（`/ws/agent` 的 status 报文）----
   * 全部**可空，`null` 表示"盒子没报过"**，不是 0、也不是 false。
   * 前端必须照这个语义显示"未上报"：把"没报过"显示成"0 帧"，
   * 会让人去拆一台好摄像头。 */
  /** 盒子认为自己的推流连接是活的 */
  agent_streaming: boolean | null
  /** 盒子上的推流开关。**在线却没画面时第一个要看的就是它** */
  agent_stream_enabled: boolean | null
  /** 盒子上的实时识别开关（推流帧先在本机推理画框） */
  agent_live_detection: boolean | null
  /** 本次进程累计推上来的帧数（盒子重启归零） */
  agent_frames_sent: number | null
  /** 累计上报成功的识别结果条数 */
  agent_uploads: number | null
  /** 还压在重试队列里的结果条数（>0 说明断过网） */
  agent_pending_uploads: number | null
  /** 最近一次错误的中文描述。**不会被自动清空**，显示时要带上时刻 */
  agent_last_error: string | null
  agent_last_error_at: string | null
  /** 最后一次收到 status 报文的时刻（判断上面这些数字新不新） */
  agent_status_at: string | null
  /**
   * 盒子当前实际在用的画面来源：`picamera2`（CSI 排线摄像头）/
   * `opencv`（USB 摄像头）/ `simulated`（模拟图，即 test_media 里的训练照片）。
   * `simulated` 表示看到的画面不是真实摄像头，网页要据此给出醒目提示。
   */
  agent_camera_backend: string | null
}

/** POST /api/devices/bind —— 用户在网页上输入配对码 */

/** GET /api/devices/pending —— 正在等待配对的盒子（「我的设备」页的敲门提示）。
 * 后端**不下发配对码**（码 = 绑定凭证），这里只有"门铃响了"的事实。 */
export interface DevicePendingOut {
  id: number
  name: string
  model: string | null
  agent_version: string | null
  backend: string | null
  last_seen_at: string | null
  /** 配对码过期时刻（ISO）。前端据此显示"还有约 X 分钟"，过期即从列表消失 */
  pairing_expires_at: string
}
export interface DeviceBindPayload {
  code: string
}

/** PATCH /api/devices/{id} —— 只传要改的字段（见 client.ts 的 patch 说明） */
export interface DeviceUpdatePayload {
  name?: string
  plot_id?: number | null
}

/**
 * GET /api/devices/{id}/stream?ticket=...
 *
 * `<img src>` 无法设置 `Authorization` 头，所以实时画面走这个**票据**地址：
 * 票据短寿命（120 秒）、限定用途（只能看这一路流）、限定设备。
 */
export interface DeviceStreamTicketOut {
  ticket: string
  expires_in: number
}

/* --------------------------- 模型注册表 --------------------------- */

export type ModelSource = 'weights' | 'runs'

export interface ModelOut {
  id: number
  name: string
  path: string
  arch: string
  map50: number | null
  map50_95: number | null
  epochs: number | null
  source: ModelSource | string
  profile: string | null
  is_default: boolean
  exists: boolean
  trained_at: string | null
}

export interface ModelRegistryOut {
  current: string | null
  recommended_id: number | null
  items: ModelOut[]
}

/* --------------------------- 虫害知识库 --------------------------- */

export interface SpeciesOut {
  class_id: number
  en_name: string
  zh_name: string
  host_crops: string
  damage: string
  control: string
  threshold_desc: string
  image_path: string | null
}

export interface SpeciesDisclaimerOut {
  disclaimer: string[]
}

/* ------------------------------- 检测 ------------------------------- */

export interface Detection {
  class_id: number
  class_name: string
  zh_name: string
  score: number
  bbox: [number, number, number, number]
  track_id: number | null
  /**
   * 帧号。**只在 REST 历史明细里有，WebSocket 实时帧里没有**
   * （实时流本来就是"当前这一帧"，没有编号的必要）。
   *
   * `GET /api/tasks/{id}/detections` 返回的每个框都带它，
   * 且该接口支持 `?frame_index=N` 过滤 —— 视频任务的明细要定位到"第几帧"就靠这个字段。
   * 所以这里是**可选**而不是必填：写成必填等于对实时流撒谎。
   */
  frame_index?: number
}

/** {中文名: 数量} */
export type CountMap = Record<string, number>

export interface ImageDetectionOut {
  task_id: number
  image_url: string
  annotated_url: string
  width: number
  height: number
  detections: Detection[]
  counts: CountMap
  frame_total: number
  unique_objects: number
  inference_ms: number
}

export interface BatchItemOut {
  filename: string
  image_url: string | null
  width: number | null
  height: number | null
  detections: Detection[]
  counts: CountMap
  frame_total: number
  inference_ms: number
  error: string | null
}

export interface BatchDetectionOut {
  task_id: number
  total: number
  succeeded: number
  failed: number
  total_objects: number
  counts: CountMap
  items: BatchItemOut[]
  elapsed_ms: number
}

export interface VideoTaskAcceptedOut {
  task_id: number
  status: TaskStatus
  source_name: string
  total_frames: number
}

/* ------------------------------- 历史 ------------------------------- */

export type TaskMode = 'image' | 'video' | 'camera' | 'folder' | 'local' | 'device'
export type TaskStatus = 'pending' | 'running' | 'done' | 'failed'

export interface TaskOut {
  id: number
  mode: TaskMode | string
  status: TaskStatus | string
  source_name: string | null
  model_id: number | null
  plot_id: number | null
  /**
   * 来源设备（绑定的树莓派）。人工上传与本机抓拍为 `null`。
   * `SET NULL` 语义：解绑/删设备不会连带删掉历史结果，那些任务只是不再指向某台设备。
   */
  device_id: number | null
  conf: number | null
  iou: number | null
  started_at: string | null
  finished_at: string | null
  frame_count: number | null
  total_frames: number | null
  duration_ms: number | null
  total_objects: number | null
  unique_objects: number | null
  result_image_path: string | null
  result_video_path: string | null
  error: string | null
}

/** GET /api/tasks/{id} —— TaskOut 加上 counts 与 progress */
export interface TaskDetailOut extends TaskOut {
  counts: CountMap
  progress: number
}

/* ------------------------------- 告警 ------------------------------- */

export type AlertLevel = 'info' | 'warn' | 'critical'

/** GET /api/alerts */
export interface AlertOut {
  id: number
  plot_id: number | null
  task_id: number | null
  level: AlertLevel | string
  rule: string | null
  message: string
  value: number | null
  ts: string | null
  acked: boolean
}

/** 规则文件里的一条（字段可变，故用宽松类型；仅用于展示"为什么会报警"） */
export interface AlertRule {
  name?: string
  metric?: string
  window?: string
  class_name?: string | null
  plot_id?: number | null
  threshold?: number
  level?: string
  message?: string
}

/** GET /api/alerts/rules */
export interface AlertRulesOut {
  rules: AlertRule[]
  webhook?: { enabled?: boolean; url?: string; timeout_s?: number }
}

export interface PageOut<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface TaskCountsOut {
  task_id: number
  counts: CountMap
  total_objects: number
  unique_objects: number
  progress: number
  result_video_path: string | null
  result_image_path: string | null
  detection_rows: number
}

export interface ReportFormat {
  format: 'xlsx' | 'docx' | 'html'
}

/* ------------------------------- 统计 ------------------------------- */

export type StatsRange = '7d' | '30d' | '24h' | 'all'

export interface ClassCountOut {
  zh_name: string
  en_name: string
  count: number
}

export interface DayCountOut {
  date: string
  count: number
}

export interface StatsSummaryOut {
  range: StatsRange | string
  total_tasks: number
  total_objects: number
  unique_objects: number
  by_class: ClassCountOut[]
  by_day: DayCountOut[]
  by_hour: number[]
  plot_count: number
}

/* --------------------------- WebSocket --------------------------- */

export type DetectionSocketState =
  | 'idle'
  | 'connecting'
  | 'open'
  | 'ready'
  | 'reconnecting'
  | 'closed'
  | 'error'

export interface WsReadyMessage {
  type: 'ready'
  user: UserOut
  detector: Record<string, unknown>
  params: WsParams
  stream: {
    target_fps: number
    jpeg_quality: number
    max_sessions: number
  }
}

export interface WsParams {
  conf: number
  iou: number
}

export interface WsTaskStartedMessage {
  type: 'task_started'
  session_id: number
}

export interface WsFrameResultMessage {
  type: 'frame_result'
  detections: Detection[]
  seq: number
  frame_total: number
  frame_by_class: CountMap
  total_objects: number
  unique_objects: number
  unique_by_class: CountMap
  active_tracks: number
  inference_ms: number
  elapsed_s: number
}

export interface WsParamsUpdatedMessage {
  type: 'params_updated'
  params: WsParams
}

export interface WsModelChangedMessage {
  type: 'model_changed'
  detector: Record<string, unknown>
}

export interface WsTaskFinishedMessage {
  type: 'task_finished'
  session_id: number
  frame_count: number
  total_objects: number
  unique_objects: number
  elapsed_s: number
}

export interface WsPongMessage {
  type: 'pong'
}

export type WsErrorCode =
  | 'auth_required'
  | 'busy'
  | 'unauthorized'
  | 'model_not_loaded'
  | 'bad_frame'
  | 'unknown_type'

export interface WsErrorMessage {
  type: 'error'
  code: WsErrorCode | string
  message: string
}

export type WsInboundMessage =
  | WsReadyMessage
  | WsTaskStartedMessage
  | WsFrameResultMessage
  | WsParamsUpdatedMessage
  | WsModelChangedMessage
  | WsTaskFinishedMessage
  | WsPongMessage
  | WsErrorMessage

/* --------------------------- 前端内部类型 --------------------------- */

export type DetectMode = 'image' | 'batch' | 'video' | 'camera'

/** 检测表格的一行 */
export interface DetectionRow {
  key: string
  /** 画面标识：文件名或帧号（绝不放 dataURL） */
  frameLabel: string
  frameIndex: number
  classId: number
  className: string
  zhName: string
  score: number
  bbox: [number, number, number, number]
  trackId: number | null
}

export interface ApiErrorShape {
  detail?: string | { msg?: string }[] | unknown
  message?: string
  code?: string
}
