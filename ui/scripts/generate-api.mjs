#!/usr/bin/env node
/**
 * generate-api.mjs — 由契约重生成 `src/lib/api/schema.d.ts`。
 *
 * 契约 `server/contracts/openapi.yaml` 是宿主 API 形状的唯一来源。
 * 生成物入库，故不跑生成器也能 `tsc`。
 * `--check` 先写到临时目录再逐字节比较：生成物过期即红。
 *
 * 为什么另装一套工具链：UI 编译器是 TypeScript 7。
 * openapi-typescript 7.13.0 需要 TypeScript 5 的编译器 API。
 * `scripts/api-codegen` 固定这两个包，生成器见不到 TypeScript 7。
 * UI 源码仍只用根目录的 TypeScript 7。
 *
 * 退出码：
 *   0  已写入或已最新
 *   1  生成物与入库文件不一致（漂移）
 *   2  配置错误（缺契约、缺工具链、输出为空）
 *
 * 用法：
 *   node scripts/generate-api.mjs
 *   node scripts/generate-api.mjs --check
 */

import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const UI_ROOT = resolve(HERE, '..')
const CONTRACT = resolve(UI_ROOT, '..', 'server', 'contracts', 'openapi.yaml')
const OUTPUT = join(UI_ROOT, 'src', 'lib', 'api', 'schema.d.ts')
const CLI = join(
  UI_ROOT,
  'scripts',
  'api-codegen',
  'node_modules',
  'openapi-typescript',
  'bin',
  'cli.js',
)

class GateError extends Error {
  constructor(message, code) {
    super(message)
    this.code = code
  }
}

function configError(message) {
  // 抛错，不 exit：process.exit 会跳过清理临时目录的 finally。
  throw new GateError(`CONFIG ERROR: ${message}`, 2)
}

function generate(target) {
  if (!existsSync(CONTRACT)) configError(`contract is missing: ${CONTRACT}`)
  if (!existsSync(CLI)) {
    configError(
      'generator toolchain is not installed; run `npm install --prefix scripts/api-codegen`',
    )
  }
  try {
    execFileSync(process.execPath, [CLI, CONTRACT, '-o', target], {
      cwd: UI_ROOT,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
  } catch (error) {
    throw new GateError(`generator failed: ${String(error.stderr ?? error.message)}`, 1)
  }
  if (!existsSync(target) || statSync(target).size === 0) {
    configError(`generator produced no output at ${target}`)
  }
}

function main() {
  if (!process.argv.includes('--check')) {
    generate(OUTPUT)
    console.log(`generate-api: wrote ${OUTPUT}`)
    return 0
  }

  const temp = mkdtempSync(join(tmpdir(), 'generate-api-'))
  const candidate = join(temp, 'schema.d.ts')
  try {
    generate(candidate)
    if (!existsSync(OUTPUT)) {
      console.error(`generate-api: DRIFT: ${OUTPUT} does not exist; run npm run generate:api`)
      return 1
    }
    const expected = readFileSync(candidate)
    const actual = readFileSync(OUTPUT)
    if (Buffer.compare(expected, actual) !== 0) {
      console.error(`generate-api: DRIFT: ${OUTPUT} is stale against ${CONTRACT}`)
      console.error('generate-api: run `npm run generate:api` and commit the result')
      return 1
    }
    console.log(`generate-api: up to date (${actual.length} B)`)
    return 0
  } finally {
    rmSync(temp, { recursive: true, force: true })
  }
}

try {
  process.exit(main())
} catch (error) {
  if (error instanceof GateError) {
    console.error(`generate-api: ${error.message}`)
    process.exit(error.code)
  }
  throw error
}
