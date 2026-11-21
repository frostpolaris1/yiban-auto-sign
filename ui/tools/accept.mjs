/**
 * **功能**
 * 统一验收 harness：复用既有 `npm run verify`（含各门 self-test＋check:api），
 * 再补 behavior＋shots(win32)，输出机器可读汇总＋非零退出码。
 * 任何实现者（主会话或外包模型）提交前必须 `npm run accept` 全绿。
 *
 * **归属**
 * `ui/tools` 走查/验收工具链；不被 src 引用。
 *
 * **口径**
 * 不重复造门：verify 已是单源全门；本 harness 只在其上补行为与视觉。
 * linux 基线与 a11y 需 WSL/服务，另跑（见 tools/README）；本 harness 只跑本机密闭项。
 * 任一失败即退出 1 并标红该项；不静默跳过。
 */
import { spawnSync } from 'node:child_process'

const STEPS = [
  ['verify', 'npm', ['run', 'verify']],
  ['behavior', 'npm', ['run', 'test:behavior']],
  ['shots-win32', 'npm', ['run', 'test:shots']],
]

const results = []
let failed = 0
for (const [name, cmd, args] of STEPS) {
  const r = spawnSync(cmd, args, { encoding: 'utf8', shell: process.platform === 'win32' })
  const ok = r.status === 0
  if (!ok) failed++
  results.push({ gate: name, rc: r.status, ok })
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name} (rc=${r.status})`)
  if (!ok) console.log((r.stdout || '').split('\n').slice(-14).join('\n'), (r.stderr || '').split('\n').slice(-6).join('\n'))
}
console.log(JSON.stringify({ failed, total: STEPS.length, results }))
process.exit(failed ? 1 : 0)