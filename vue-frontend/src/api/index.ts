/**
 * API 层统一出口。
 * 只做转发；所有真实数据均来自后端，前端不产生任何假数据。
 */

export * from './client'
export * as alertsApi from './alerts'
export * as authApi from './auth'
export * as detectionApi from './detection'
export * as devicesApi from './devices'
export * as localCameraApi from './localCamera'
export * as modelsApi from './models'
export * as tasksApi from './tasks'
export * as statsApi from './stats'
export * as speciesApi from './species'
export * as systemApi from './system'
