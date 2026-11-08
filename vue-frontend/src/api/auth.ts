import { get, post, upload, toNumber, type UploadOptions } from './client'
import type {
  AvatarResponse,
  LoginPayload,
  LoginResponse,
  MessageResponse,
  UserOut
} from '@/types'

export function login(payload: LoginPayload): Promise<LoginResponse> {
  return post<LoginResponse>('/auth/login', payload)
}

export function register(payload: LoginPayload): Promise<UserOut> {
  return post<UserOut>('/auth/register', payload)
}

export function me(): Promise<UserOut> {
  return get<UserOut>('/auth/me')
}

export function changePassword(oldPassword: string, newPassword: string): Promise<MessageResponse> {
  return post<MessageResponse>('/auth/password', {
    old_password: oldPassword,
    new_password: newPassword
  })
}

export function logout(): Promise<MessageResponse> {
  return post<MessageResponse>('/auth/logout')
}

/** 后端返回的 avatar_path 可能是绝对路径，统一归一化为可访问 URL */
export function normalizeMediaUrl(raw: string | null | undefined): string {
  if (!raw) return ''
  if (/^https?:\/\//i.test(raw) || raw.startsWith('data:') || raw.startsWith('blob:')) return raw
  return raw.startsWith('/') ? raw : `/${raw}`
}

export function uploadAvatar(file: File, options: UploadOptions = {}): Promise<AvatarResponse> {
  const form = new FormData()
  form.append('file', file, file.name)
  return upload<AvatarResponse>('/auth/avatar', form, options)
}

export { toNumber }
