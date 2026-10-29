/**
 * **功能**
 * 图标来源门：图标只从 `lucide-react` 取。
 *
 * **归属**
 * `scripts/tests`：纯 Node 单测，读 `src/` 下的 `.ts` 与 `.tsx` 源码。
 *
 * **口径**
 * 门判四件事：
 * 一，`Icon.tsx` 必须有 `lucide-react` 的具名导入，type-only 导入不算值；
 * 二，`ICON_COMPONENTS` 的每个值都是该导入的组件，注册表不得为空；
 * 三，`IconName` 取 `keyof typeof ICON_COMPONENTS`，名字域只有一处来源；
 * 四，`src/` 里除品牌标记外没有第二处手抄几何。
 * 第四条把门的对象对齐实际改动面：只扫 `Icon.tsx` 会漏掉别处新增的图标文件。
 * 品牌标记按文件登记，内容由 `d` 的哈希钉住；改内容或加路径即红。
 * 门对每种退化源码自测，证明它能变红。
 */

import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import { parse } from '@babel/parser'

const here = dirname(fileURLToPath(import.meta.url))
const uiRoot = join(here, '..', '..')
const srcRoot = join(uiRoot, 'src')
const ICON_FILE = join(srcRoot, 'components', 'ui', 'Icon.tsx')
const ICON_SOURCE = readFileSync(ICON_FILE, 'utf8')

const LUCIDE = 'lucide-react'
const REGISTRY = 'ICON_COMPONENTS'
/** 手抄几何的元素名。图标的形状只允许由 lucide 组件提供。 */
const GEOMETRY = new Set(['path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'ellipse'])
/**
 * 品牌标记：按文件登记，内容钉住。
 * 值是 Adminator 品牌标记那一条 `d` 的 sha256；形状来自设计资产，不是图标。
 */
const BRAND_MARK = 'components/ui/BrandMark.tsx'
const BRAND_MARK_SHA = 'c63e7ea9358620e386fe5bbda00cb3fb78d39a6504738d9847767076308eb196'

/** unwrap 取表达式内核。`as const` 与 `satisfies` 都是包装。 */
function unwrap(node) {
  let current = node
  while (
    current &&
    (current.type === 'TSAsExpression' ||
      current.type === 'TSSatisfiesExpression' ||
      current.type === 'TSTypeAssertion' ||
      current.type === 'TSNonNullExpression' ||
      current.type === 'ParenthesizedExpression')
  ) {
    current = current.expression
  }
  return current
}

/** statements 展开顶层语句，含 `export const`。 */
function statements(ast) {
  const out = []
  for (const node of ast.program.body) {
    if (node.type === 'ExportNamedDeclaration' && node.declaration) {
      out.push(node.declaration)
    } else {
      out.push(node)
    }
  }
  return out
}

/** lucideBindings 返回 `lucide-react` 具名导入的本地名。type-only 导入不算值。 */
function lucideBindings(ast) {
  const names = new Set()
  for (const node of statements(ast)) {
    if (node.type !== 'ImportDeclaration' || node.source.value !== LUCIDE) {
      continue
    }
    if (node.importKind === 'type') {
      continue
    }
    for (const spec of node.specifiers) {
      if (spec.type === 'ImportSpecifier' && spec.importKind !== 'type') {
        names.add(spec.local.name)
      }
    }
  }
  return names
}

/** registryNode 返回 `ICON_COMPONENTS` 的对象字面量。找不到返回 null。 */
function registryNode(ast) {
  for (const node of statements(ast)) {
    if (node.type !== 'VariableDeclaration') {
      continue
    }
    for (const decl of node.declarations) {
      if (decl.id.type !== 'Identifier' || decl.id.name !== REGISTRY) {
        continue
      }
      const init = unwrap(decl.init)
      return init && init.type === 'ObjectExpression' ? init : null
    }
  }
  return null
}

/** hasKeyofRegistry 判断 `IconName` 是 `keyof typeof ICON_COMPONENTS`。操作数必须点名。 */
function hasKeyofRegistry(ast) {
  for (const node of statements(ast)) {
    if (node.type !== 'TSTypeAliasDeclaration' || node.id.name !== 'IconName') {
      continue
    }
    const annotation = node.typeAnnotation
    return (
      annotation.type === 'TSTypeOperator' &&
      annotation.operator === 'keyof' &&
      annotation.typeAnnotation.type === 'TSTypeQuery' &&
      annotation.typeAnnotation.exprName.type === 'Identifier' &&
      annotation.typeAnnotation.exprName.name === REGISTRY
    )
  }
  return false
}

/** walk 深度优先遍历全部子节点。 */
function walk(node, visit) {
  if (!node || typeof node.type !== 'string') {
    return
  }
  visit(node)
  for (const key of Object.keys(node)) {
    if (key === 'loc' || key === 'leadingComments' || key === 'trailingComments') {
      continue
    }
    const value = node[key]
    if (Array.isArray(value)) {
      for (const item of value) {
        walk(item, visit)
      }
    } else if (value && typeof value.type === 'string') {
      walk(value, visit)
    }
  }
}

/** parseSource 解析 TSX 源码。失败时返回错误文本。 */
function parseSource(source) {
  try {
    return parse(source, { sourceType: 'module', plugins: ['typescript', 'jsx'] })
  } catch (error) {
    return `源码无法解析：${error.message}`
  }
}

/** pathData 返回源码里全部 `d` 属性的字符串值。 */
export function pathData(source) {
  const ast = parseSource(source)
  if (typeof ast === 'string') {
    return ast
  }
  const values = []
  walk(ast.program, (node) => {
    if (
      node.type === 'JSXAttribute' &&
      node.name.type === 'JSXIdentifier' &&
      node.name.name === 'd' &&
      node.value?.type === 'StringLiteral'
    ) {
      values.push(node.value.value)
    }
  })
  return values
}

/**
 * geometryFindings 返回源码里的手抄几何。空数组表示通过。
 * 纯函数：自测直接把退化源码喂进来。
 */
export function geometryFindings(source) {
  const ast = parseSource(source)
  if (typeof ast === 'string') {
    return [ast]
  }
  const findings = []
  walk(ast.program, (node) => {
    if (node.type === 'JSXOpeningElement' && node.name.type === 'JSXIdentifier') {
      if (GEOMETRY.has(node.name.name)) {
        findings.push(`手抄几何：<${node.name.name}>`)
      }
    }
    if (node.type === 'JSXAttribute' && node.name.type === 'JSXIdentifier') {
      if (node.name.name === 'd') {
        findings.push('手抄路径：d 属性')
      }
    }
  })
  return [...new Set(findings)]
}

/**
 * brandFindings 返回品牌标记文件的内容偏差。空数组表示与登记一致。
 * 纯函数：自测直接把退化源码喂进来。
 */
export function brandFindings(source) {
  const values = pathData(source)
  if (typeof values === 'string') {
    return [values]
  }
  if (values.length !== 1) {
    return [`品牌标记必须只有一条 path，实有 ${values.length} 条`]
  }
  const actual = createHash('sha256').update(values[0]).digest('hex')
  if (actual !== BRAND_MARK_SHA) {
    return [`品牌标记的 path 内容已变：${actual}`]
  }
  return []
}

/**
 * findingsOf 返回 `Icon.tsx` 源码的违规说明。空数组表示通过。
 * 纯函数：自测直接把退化源码喂进来。
 */
export function findingsOf(source) {
  const ast = parseSource(source)
  if (typeof ast === 'string') {
    return [ast]
  }
  const findings = []
  const bindings = lucideBindings(ast)

  if (bindings.size === 0) {
    findings.push(`没有 ${LUCIDE} 的具名导入`)
  }

  const registry = registryNode(ast)
  if (!registry) {
    findings.push(`缺少 ${REGISTRY} 对象字面量`)
  } else if (registry.properties.length === 0) {
    findings.push(`${REGISTRY} 为空：扫空不得当绿`)
  } else {
    for (const prop of registry.properties) {
      const name = prop.key.type === 'Identifier' ? prop.key.name : String(prop.key.value ?? '?')
      const value = unwrap(prop.value)
      if (value?.type !== 'Identifier' || !bindings.has(value.name)) {
        findings.push(`图标 ${name} 的值不是 ${LUCIDE} 的组件`)
      }
    }
  }

  if (!hasKeyofRegistry(ast)) {
    findings.push('IconName 必须取注册表的键（keyof typeof）')
  }

  return [...findings, ...geometryFindings(source)]
}

/** sourceFiles 递归列出 `src/` 下的 `.ts` 与 `.tsx`，返回 posix 相对路径。 */
function sourceFiles(dir, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === '.git') {
      continue
    }
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      sourceFiles(full, out)
    } else if (entry.isFile() && (entry.name.endsWith('.ts') || entry.name.endsWith('.tsx'))) {
      out.push(relative(srcRoot, full).split(sep).join('/'))
    }
  }
  return out
}

test('Icon.tsx 的图标只从 lucide-react 取', () => {
  assert.deepEqual(findingsOf(ICON_SOURCE), [], `${ICON_FILE} 违反图标来源口径`)
})

test('品牌标记的图形按内容钉住', () => {
  const source = readFileSync(join(srcRoot, BRAND_MARK), 'utf8')
  assert.deepEqual(brandFindings(source), [])
  // 突变：改一个字符、加一条 path，都必须红。
  assert.ok(brandFindings(source.replace('M14.747', 'M14.748')).length > 0, '改内容未变红')
  assert.ok(
    brandFindings(source.replace('</svg>', '<path d="M0 0h1v1H0z" /></svg>')).length > 0,
    '加路径未变红',
  )
})

test('src 里除登记的品牌标记外没有第二处手抄 svg', () => {
  const files = sourceFiles(srcRoot)
  assert.ok(files.length > 0, 'src/ 下没有发现源码：扫空不得当绿')
  assert.ok(files.includes(BRAND_MARK), `登记的品牌标记不存在：${BRAND_MARK}`)

  const findings = []
  let scanned = 0
  for (const name of files) {
    if (name === BRAND_MARK) {
      continue
    }
    const source = readFileSync(join(srcRoot, name), 'utf8')
    scanned += 1
    for (const finding of geometryFindings(source)) {
      findings.push(`src/${name}: ${finding}`)
    }
  }
  assert.equal(scanned, files.length - 1, '检查面必须等于扫描面')
  assert.ok(scanned > 0, '没有任何文件被检查：扫空不得当绿')
  assert.deepEqual(findings, [])
})

test('门对每种退化形状都能变红', () => {
  const base = [
    "import { Bell } from 'lucide-react'",
    'const ICON_COMPONENTS = { bell: Bell } as const',
    'type IconName = keyof typeof ICON_COMPONENTS',
    'function Icon({ name }: { name: IconName }) { return null }',
  ].join('\n')
  assert.deepEqual(findingsOf(base), [], '合规样例不得误报')

  const cases = [
    [
      '手抄 path',
      base.replace(
        'const ICON_COMPONENTS',
        'const drawn = <path d="M0 0h24v24H0z" />\nconst ICON_COMPONENTS',
      ),
    ],
    ['手抄 circle', base.replace('return null', 'return <svg><circle cx="1" cy="1" r="1" /></svg>')],
    ['缺 lucide 导入', base.replace("import { Bell } from 'lucide-react'\n", '')],
    ['注册表为空', base.replace('{ bell: Bell }', '{}')],
    ['值不是 lucide 组件', base.replace('{ bell: Bell }', '{ bell: () => null }')],
    [
      'IconName 不取注册表',
      base.replace('type IconName = keyof typeof ICON_COMPONENTS', 'type IconName = "bell"'),
    ],
    [
      'IconName 取别的表',
      base.replace('keyof typeof ICON_COMPONENTS', 'keyof typeof OTHER_TABLE'),
    ],
    [
      'type-only 导入冒充值',
      base.replace("import { Bell } from 'lucide-react'", "import { type Bell } from 'lucide-react'"),
    ],
  ]
  for (const [label, source] of cases) {
    assert.ok(findingsOf(source).length > 0, `退化形状未变红：${label}`)
  }
})
