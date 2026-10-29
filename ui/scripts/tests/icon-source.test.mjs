/**
 * **功能**
 * 图标来源门：图标只从 `lucide-react` 取。
 *
 * **归属**
 * `scripts/tests`：纯 Node 单测，读 `src/` 下的 `.ts`、`.tsx` 与 `.css`。
 *
 * **口径**
 * 门判六件事：
 * 一，`Icon.tsx` 必须有 `lucide-react` 的具名导入，type-only 导入不算值；
 * 二，`ICON_COMPONENTS` 的每个值都是该导入的组件，注册表不得为空；
 * 三，`IconName` 取 `keyof typeof ICON_COMPONENTS`，名字域只有一处来源；
 * 四，`src/` 的 TSX 里除品牌标记外没有手抄几何；
 * 五，`src/` 的 CSS 里每个 `data:image/svg` 都登记在册，登记项＝文件路径＋
 *    data URI 的 sha256＋理由；原生控件必须走 CSS background，这是唯一豁免；
 * 六，`src/` 内模块导入只来自登记表；`lucide-react` 只许出现在 `Icon.tsx`。
 *
 * 第四条把门的对象对齐实际改动面：只扫 `Icon.tsx` 会漏掉别处新增的图标文件。
 * 品牌标记只允许恰一条静态 `<path d="…">`；其它几何、动态 `d`、内容变化都红。
 * 第五条补上第二条来源：`adminator.css` 用内联 SVG 画 select 箭头。
 * 第六条拦第二套图标库：允许的裸模块逐条列出，不用通配。
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

/**
 * 图标库来源白名单：只列本仓实际使用的非图标依赖。
 * 没有通配。新增依赖必须在此逐条登记，否则门红。
 */
const ALLOWED_BARE_IMPORTS = new Set([
  'react',
  'react-dom',
  'react-dom/client',
  '@tanstack/react-query',
  '@tanstack/react-router',
  'radix-ui',
  'sonner',
  '@formkit/auto-animate',
  '@formkit/auto-animate/react',
  '@number-flow/react',
])

/** 唯一允许导入 `lucide-react` 的文件。 */
const LUCIDE_FILE = 'components/ui/Icon.tsx'

/**
 * CSS 内联 SVG 登记表。每一项都必须在对文件的扫描里出现。
 * sha256 取 `data:image/svg+xml…` 到最近的引号或右括号之前的原文。
 */
const CSS_DATA_URI_REGISTRY = [
  {
    file: 'styles/adminator.css',
    sha256: '309cf69c9d2c4f2c0407964fd0d74191c0faa1aa9d109a2c76f9e6588121d591',
    reason:
      '原生 select 的下拉指示器，必须走 CSS background；与 lucide chevronDown 同形，改时同步两处',
  },
]

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
 * 只允许恰一条静态 `<path d="…">`：其它几何、动态 `d`、多处 `d` 都红。
 * 纯函数：自测直接把退化源码喂进来。
 */
export function brandFindings(source) {
  const ast = parseSource(source)
  if (typeof ast === 'string') {
    return [ast]
  }
  const shapes = []
  const dAttrs = []
  walk(ast.program, (node) => {
    if (
      node.type === 'JSXOpeningElement' &&
      node.name.type === 'JSXIdentifier' &&
      GEOMETRY.has(node.name.name)
    ) {
      shapes.push(node)
    }
    if (
      node.type === 'JSXAttribute' &&
      node.name.type === 'JSXIdentifier' &&
      node.name.name === 'd'
    ) {
      dAttrs.push(node)
    }
  })

  if (shapes.length !== 1) {
    return [`品牌标记必须只有一条几何，实有 ${shapes.length} 条`]
  }
  const shape = shapes[0]
  if (shape.name.name !== 'path') {
    return [`品牌标记的唯一几何必须是 path，实为 <${shape.name.name}>`]
  }
  if (dAttrs.length !== 1) {
    return [`品牌标记必须只有一个 d 属性，实有 ${dAttrs.length} 个`]
  }
  const value = dAttrs[0].value
  if (value?.type !== 'StringLiteral') {
    return ['品牌标记的 d 必须是静态字符串，不得用表达式']
  }
  const actual = createHash('sha256').update(value.value).digest('hex')
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

/**
 * cssDataUriRecords 返回 CSS 文本里的内联 SVG 原文。
 * 原文从 `data:image/svg+xml` 起，到最近的引号或右括号止。
 */
export function cssDataUriRecords(source) {
  const pattern = /data:image\/svg\+xml[^"')]*/g
  const out = []
  let match
  while ((match = pattern.exec(source)) !== null) {
    out.push(match[0])
  }
  return out
}

/**
 * cssDataUriFindings 返回未登记的 CSS 内联 SVG。空数组表示通过。
 * `files` 是 `{ path, text }` 列表，`path` 相对 `src/`。
 * 纯函数：自测直接把退化输入喂进来。
 */
export function cssDataUriFindings(files, registry = CSS_DATA_URI_REGISTRY) {
  if (files.length === 0) {
    return ['扫描集为空：扫空不得当绿']
  }
  const findings = []
  const keys = new Map()
  for (const entry of registry) {
    if (!entry.reason || !entry.reason.trim()) {
      findings.push(`登记项缺理由：${entry.file}`)
    }
    keys.set(`${entry.file}\u0000${entry.sha256}`, entry)
  }

  const seen = new Set()
  for (const file of files) {
    for (const uri of cssDataUriRecords(file.text)) {
      const sha = createHash('sha256').update(uri).digest('hex')
      const key = `${file.path}\u0000${sha}`
      if (!keys.has(key)) {
        findings.push(`src/${file.path}: 未登记的 svg data URI（sha256 ${sha}）`)
      } else {
        seen.add(key)
      }
    }
  }

  for (const [key, entry] of keys) {
    if (!seen.has(key)) {
      findings.push(`登记项已失效：${entry.file} 里找不到 sha256 ${entry.sha256}`)
    }
  }
  return findings
}

/**
 * importFindings 返回一个源文件的模块导入偏差。空数组表示通过。
 * 允许的裸模块只有 `ALLOWED_BARE_IMPORTS`；`lucide-react` 只许在 `Icon.tsx`。
 * 纯函数：自测直接把退化源码喂进来。
 */
export function importFindings(file, source) {
  const ast = parseSource(source)
  if (typeof ast === 'string') {
    return [ast]
  }
  const findings = []
  const specifiers = []
  walk(ast.program, (node) => {
    if (
      (node.type === 'ImportDeclaration' ||
        node.type === 'ExportNamedDeclaration' ||
        node.type === 'ExportAllDeclaration') &&
      node.source?.value
    ) {
      specifiers.push(node.source.value)
    }
    if (
      (node.type === 'ImportExpression' && node.source?.type === 'StringLiteral') ||
      (node.type === 'CallExpression' &&
        node.callee?.type === 'Import' &&
        node.arguments[0]?.type === 'StringLiteral')
    ) {
      specifiers.push(node.type === 'ImportExpression' ? node.source.value : node.arguments[0].value)
    }
  })

  for (const spec of specifiers) {
    if (spec.startsWith('.') || spec.startsWith('/') || spec.startsWith('@/')) {
      continue
    }
    if (spec === LUCIDE) {
      if (file !== LUCIDE_FILE) {
        findings.push(`${LUCIDE} 只允许在 ${LUCIDE_FILE} 导入，本文件是 ${file}`)
      }
      continue
    }
    if (!ALLOWED_BARE_IMPORTS.has(spec)) {
      findings.push(`未登记的模块：${spec}（图标只允许 ${LUCIDE}）`)
    }
  }
  return findings
}

/** sourceFiles 递归列出 `src/` 下的指定扩展名，返回 posix 相对路径。 */
function sourceFiles(dir, extensions, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === '.git') {
      continue
    }
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      sourceFiles(full, extensions, out)
    } else if (entry.isFile() && extensions.some((ext) => entry.name.endsWith(ext))) {
      out.push(relative(srcRoot, full).split(sep).join('/'))
    }
  }
  return out
}

const MODULE_EXTENSIONS = ['.ts', '.tsx']
const STYLE_EXTENSIONS = ['.css']

test('Icon.tsx 的图标只从 lucide-react 取', () => {
  assert.deepEqual(findingsOf(ICON_SOURCE), [], `${ICON_FILE} 违反图标来源口径`)
})

test('品牌标记的图形按内容钉住', () => {
  const source = readFileSync(join(srcRoot, BRAND_MARK), 'utf8')
  assert.deepEqual(brandFindings(source), [])
  const mutations = [
    ['改内容', source.replace('M14.747', 'M14.748')],
    ['加路径', source.replace('</svg>', '<path d="M0 0h1v1H0z" /></svg>')],
    ['加圆', source.replace('</svg>', '<circle cx="1" cy="1" r="1" /></svg>')],
    ['改矩形', source.replace('<path', '<rect').replace('fill="currentColor"', 'fill="currentColor"')],
    ['动态 d', source.replace(/d="[^"]*"/, 'd={SHAPE}')],
    ['换掉 path 名', source.replace(/<path\b/, '<polygon')],
  ]
  for (const [label, mutated] of mutations) {
    assert.ok(brandFindings(mutated).length > 0, `品牌标记突变未变红：${label}`)
  }
})

test('src 里除登记的品牌标记外没有第二处手抄 svg', () => {
  const files = sourceFiles(srcRoot, MODULE_EXTENSIONS)
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

test('src 的 CSS 内联 svg 全部登记在册', () => {
  const names = sourceFiles(srcRoot, STYLE_EXTENSIONS)
  assert.ok(names.length > 0, 'src/ 下没有 CSS：扫空不得当绿')
  const files = names.map((name) => ({
    path: name,
    text: readFileSync(join(srcRoot, name), 'utf8'),
  }))
  const total = files.reduce((sum, file) => sum + cssDataUriRecords(file.text).length, 0)
  assert.ok(total > 0, '一个内联 svg 都没找到：登记表已失效')
  assert.deepEqual(cssDataUriFindings(files), [])
})

test('src 的模块导入只来自登记表', () => {
  const files = sourceFiles(srcRoot, MODULE_EXTENSIONS)
  assert.ok(files.length > 0, 'src/ 下没有源码：扫空不得当绿')
  const findings = []
  for (const name of files) {
    for (const finding of importFindings(name, readFileSync(join(srcRoot, name), 'utf8'))) {
      findings.push(`src/${name}: ${finding}`)
    }
  }
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

test('CSS 门对登记与未登记都能判对', () => {
  // 与 adminator.css 同形状：属性引号用 %27，外层 url() 用双引号。
  const uri = "<svg xmlns=%27http://www.w3.org/2000/svg%27><path d=%27m6 9 6 6 6-6%27/></svg>"
  const data = `data:image/svg+xml;utf8,${uri}`
  const sha = createHash('sha256').update(data).digest('hex')
  const file = { path: 'styles/x.css', text: `.sel { background-image: url("${data}"); }` }
  const registry = [{ file: 'styles/x.css', sha256: sha, reason: '测试理由' }]

  assert.deepEqual(cssDataUriFindings([file], registry), [], '登记后不得报错')
  assert.ok(cssDataUriFindings([file], []).length > 0, '未登记未变红')
  assert.ok(
    cssDataUriFindings([file], [{ ...registry[0], sha256: 'deadbeef' }]).length > 0,
    '哈希不符未变红',
  )
  assert.ok(
    cssDataUriFindings([file], [{ ...registry[0], file: 'styles/other.css' }]).length > 0,
    '文件不符未变红',
  )
  assert.ok(
    cssDataUriFindings([file], [{ ...registry[0], reason: ' ' }]).length > 0,
    '理由为空未变红',
  )
  assert.ok(
    cssDataUriFindings([], registry).length > 0,
    '空扫描集未变红',
  )
  const changed = { ...file, text: `.sel { background-image: url("${data.replace('6', '7')}"); }` }
  assert.ok(
    cssDataUriFindings([changed], registry).length > 0,
    '改了 data URI 内容未变红',
  )
})

test('导入门对图标库与 lucide 位置都能判对', () => {
  const source = "import { Button } from 'radix-ui'\nexport function X() { return Button }"
  assert.deepEqual(importFindings('components/ui/Button.tsx', source), [], '合规样例不得误报')

  const cases = [
    ['图标库', "import { FaBeer } from 'react-icons/fa'", 'components/ui/Button.tsx'],
    ['图标库作用域', "import { Bell } from '@heroicons/react/24/outline'", 'components/ui/Button.tsx'],
    ['radix 图标', "import { GearIcon } from '@radix-ui/react-icons'", 'components/ui/Button.tsx'],
    ['lucide 越界', "import { Bell } from 'lucide-react'", 'features/dashboard/DashboardView.tsx'],
    ['动态导入图标库', "const x = import('react-icons/fa')", 'components/ui/Button.tsx'],
    ['再导出图标库', "export { Bell } from '@tabler/icons-react'", 'components/ui/Button.tsx'],
  ]
  for (const [label, snippet, file] of cases) {
    const mutated = label === '动态导入图标库' || label === '再导出图标库' ? snippet : `${snippet}\n${source}`
    assert.ok(importFindings(file, mutated).length > 0, `退化形状未变红：${label}`)
  }

  assert.deepEqual(
    importFindings('components/ui/Icon.tsx', "import { Bell } from 'lucide-react'"),
    [],
    'Icon.tsx 里的 lucide 导入不得误报',
  )
  assert.deepEqual(
    importFindings('components/ui/Card.tsx', "import { Bell } from '@/components/ui/Icon'"),
    [],
    '别名导入不得误报',
  )
})
