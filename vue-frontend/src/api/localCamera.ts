import { get, post } from './client'
import type { LocalCameraCaptureOut, LocalCameraStatusOut, LocalCameraBackendOut } from '@/types'

/* ---------------------- 本机（树莓派直连）摄像头 ---------------------- */

export function status(): Promise<LocalCameraStatusOut> {
  return get<LocalCameraStatusOut>('/local-camera/status')
}

export function backends(): Promise<LocalCameraBackendOut[]> {
  return get<LocalCameraBackendOut[]>('/local-camera/backends')
}

/** 立即抓一拍。后端会把它放线程池里跑（打开摄像头 + 推理是重活），因此可能慢几秒。 */
export function captureNow(): Promise<LocalCameraCaptureOut> {
  return post<LocalCameraCaptureOut>('/local-camera/capture')
}

export function start(): Promise<LocalCameraStatusOut> {
  return post<LocalCameraStatusOut>('/local-camera/start')
}

export function stop(): Promise<LocalCameraStatusOut> {
  return post<LocalCameraStatusOut>('/local-camera/stop')
}
