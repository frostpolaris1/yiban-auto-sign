#!/usr/bin/env node
/**
 * audit-allow.mjs — npm audit 的高危/严重漏洞门，带**带期限**的临时白名单。
 *
 * **功能**
 * 在指定目录跑 `npm audit --json --audit-level=high`，按 advisory 聚合并与
 * `scripts/audit-allowlist.tsv` 比对：白名单内的 advisory 放行并打印，
 * 白名单外的仍按 high/critical 拦截。
 *
 * **归属**
 * 工具链快门（`scripts/tooling-gate.sh`）的第 5 道检查；不参与运行时。
 *
 * **口径**
 * - 白名单条目必须带到期日与理由；到期当日起条目失效、门转红（不允许永久豁免）。
 * - 镜像 registry 不支持 audit 端点时退出码 2：环境错误不等于绿。
 * - 只放行 advisory 级（GHSA id）判定，不按包名放行：同一包新披露的漏洞仍会拦。
 * - `AUDIT_JSON` 环境变量可指向一份已保存的 audit JSON，用于离线自检。
 *
 * 退出码：0 通过（可能含白名单项）；1 有未豁免的 high/critical 或白名单过期；2 环境错误。
 */

import { execFileSync } from 'node:child_process'
import { readFileSync, existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const ALLOWLIST = join(HERE, 'audit-allowlist.tsv')
const BLOCKING = new Set(['high', 'critical'])

function fail(message) {
  console.error(`audit-allow: ${message}`)
  process.exit(1)
}

function envError(message) {
  console.error(`audit-allow: ${message}`)
  process.exit(2)
}

/** 读白名单：`ghsa \t 到期日 \t 理由`；# 开头与空行跳过。 */
function loadAllowlist(today) {
  if (!existsSync(ALLOWLIST)) return { allowed: new Map(), expired: [] }
  const allowed = new Map()
  const expired = []
  for (const raw of readFileSync(ALLOWLIST, 'utf8').split('\n')) {
    const line = raw.trim()
    if (line === '' || line.startsWith('#')) continue
    const [id, expires, ...reason] = line.split('\t')
    if (!id || !expires) fail(`白名单行格式错误：${line}`)
    if (expires < today) {
      expired.push({ id, expires, reason: reason.join(' ') })
      continue
    }
    allowed.set(id, { expires, reason: reason.join(' ') })
  }
  return { allowed, expired }
}

/** 从 audit JSON 里提取 advisory（GHSA id → 元数据）。 */
function collectAdvisories(report) {
  const found = new Map()
  for (const vuln of Object.values(report.vulnerabilities ?? {})) {
    for (const via of vuln.via ?? []) {
      if (typeof via !== 'object' || !via.url) continue
      const match = /GHSA-[A-Za-z0-9-]+/.exec(via.url)
      if (!match) continue
      const id = match[0]
      if (!BLOCKING.has(via.severity)) continue
      if (!found.has(id)) {
        found.set(id, { title: via.title ?? '', severity: via.severity, packages: new Set() })
      }
      found.get(id).packages.add(vuln.name)
    }
  }
  return found
}

const target = process.argv[2]
if (!target) fail('用法：node scripts/audit-allow.mjs <目录>')

// Windows 上 npm 是 npm.cmd；execFileSync 不走 shell，必须显式换名。
const NPM = process.platform === 'win32' ? 'npm.cmd' : 'npm'

let raw
if (process.env.AUDIT_JSON) {
  raw = readFileSync(process.env.AUDIT_JSON, 'utf8')
} else {
  try {
    raw = execFileSync(NPM, ['audit', '--json', '--audit-level=high'], {
      cwd: resolve(ROOT, target),
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
      maxBuffer: 64 * 1024 * 1024,
      // Windows 的 npm.cmd 必须经 shell 启动；参数是写死的，无注入面。
      shell: process.platform === 'win32',
    })
  } catch (error) {
    // npm audit 发现漏洞时退出码为 1，JSON 仍在 stdout；其它错误按环境错误处理。
    if (typeof error.stdout === 'string' && error.stdout.trim().startsWith('{')) {
      raw = error.stdout
    } else {
      envError(`npm audit 无法运行（${error.message.split('\n')[0]}）；镜像 registry 请换官方源`)
    }
  }
}

let report
try {
  report = JSON.parse(raw)
} catch {
  envError('npm audit 输出不是合法 JSON')
}
if (report.error) envError(`npm audit 报错：${report.error.summary || report.error.code || JSON.stringify(report.error)}`)

const today = new Date().toISOString().slice(0, 10)
const { allowed, expired } = loadAllowlist(today)
const advisories = collectAdvisories(report)

if (expired.length > 0) {
  for (const item of expired) {
    console.error(`audit-allow: 白名单已过期 ${item.id}（${item.expires} 到期）：${item.reason}`)
  }
  fail('白名单存在过期条目，先复核再续期')
}

const unlisted = [...advisories.entries()].filter(([id]) => !allowed.has(id))
for (const [id, item] of advisories.entries()) {
  const pkgs = [...item.packages].join(', ')
  if (allowed.has(id)) {
    const entry = allowed.get(id)
    console.log(`audit-allow: 放行 ${id}（${item.severity}，到期 ${entry.expires}）影响 ${pkgs}`)
  } else {
    console.error(`audit-allow: 拦截 ${id} ${item.severity} ${item.title}（影响 ${pkgs}）`)
  }
}

if (unlisted.length > 0) fail(`${unlisted.length} 个未豁免的高危/严重 advisory`)
console.log(`audit-allow: ok（${target}，high/critical 全部通过或已豁免）`)
