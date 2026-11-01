// ui/eslint.config.mjs — CLEAN-4 的静态检查配置。
//
// 形状：ESLint 9 维护线 + Babel 解析器。
// 为什么是 9：jsx-a11y 6.10 的 peer 只到 ESLint 9；10 等插件跟上再升。
// 为什么不用 typescript-eslint：
// ui 的编译器是 TypeScript 7（原生实现），typescript-eslint 8 的 peer 范围
// 是 <6.1；装它要破坏 tsc 的版本。Babel 解析器只看语法，不需要类型服务。
//
// 范围：只开 react-hooks 与 4 条 schema 类 a11y 规则。
// 为什么不一次全开：jsx-a11y 的交互类规则会在既有自定义控件上成片报红，
// 那属于 UX 改造批次，不混进本批（AGENTS §37a）。
//
// 口径：
//   - rules-of-hooks / exhaustive-deps 为 error：hook 规则错了运行期必炸。
//   - exhaustive-deps 的豁免必须在代码里写明理由（accounts 两处、schedule 一处）。
//   - a11y 只查「属性名/角色值/元素支持」这类客观错误，不查交互实现。

import babelParser from '@babel/eslint-parser'
import reactHooks from 'eslint-plugin-react-hooks'
import jsxA11y from 'eslint-plugin-jsx-a11y'

export default [
  {
    ignores: [
      'dist/**',
      'dist-showcase/**',
      'node_modules/**',
      'playwright-report/**',
      'test-results/**',
      'scripts/api-codegen/node_modules/**',
    ],
  },
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: {
      parser: babelParser,
      parserOptions: {
        requireConfigFile: false,
        sourceType: 'module',
        ecmaVersion: 'latest',
        babelOptions: {
          presets: [
            '@babel/preset-typescript',
            ['@babel/preset-react', { runtime: 'automatic' }],
          ],
        },
      },
    },
    plugins: {
      'react-hooks': reactHooks,
      'jsx-a11y': jsxA11y,
    },
    linterOptions: {
      // 死抑制直接判红：留着 disable 却压不住任何东西，就是在骗读者。
      reportUnusedDisableDirectives: 'error',
    },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'error',
      'jsx-a11y/alt-text': 'error',
      'jsx-a11y/aria-props': 'error',
      'jsx-a11y/aria-role': 'error',
      'jsx-a11y/aria-unsupported-elements': 'error',
    },
  },
]
