import { mergeConfig, type ConfigEnv } from 'vite'
import base from './vite.config'
export default (env: ConfigEnv) =>
  mergeConfig((base as unknown as (e: ConfigEnv) => Record<string, unknown>)(env), { server: { allowedHosts: ['develop.netbird.cloud'] } })
