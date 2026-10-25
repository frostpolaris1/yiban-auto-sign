#!/usr/bin/env node
/**
 * generate-api.mjs — regenerates `src/lib/api/schema.d.ts` from the contract.
 *
 * The contract `server/contracts/openapi.yaml` is the only source of the host
 * API shapes. The generated file is committed so `tsc` works without running
 * the generator. `--check` regenerates into a temporary directory and compares
 * bytes: a stale file fails the gate.
 *
 * Why a nested toolchain: the UI compiler is TypeScript 7. openapi-typescript
 * 7.13.0 needs the TypeScript 5 compiler API. `scripts/api-codegen` pins both
 * packages, so the generator never sees TypeScript 7. UI source code still uses
 * the root TypeScript 7 compiler only.
 *
 * Exit codes:
 *   0  written or up to date
 *   1  generated output differs from the committed file (drift)
 *   2  configuration error (missing contract, missing toolchain, empty output)
 *
 * Usage:
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
  // Throw, never exit: process.exit skips the finally that removes the
  // temporary directory.
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
