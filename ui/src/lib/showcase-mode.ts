/**
 * **功能**
 * 展示构建（`VITE_SHOWCASE=1`）的判定与只读原因文案。
 *
 * **归属**
 * `lib` 工具层：纯常量，无 React、无网络、无 DOM。
 *
 * **口径**
 * 判定必须全等字符串 `'1'`：Vite 注入的是字符串，`"0"` 也是真值。
 * 读取用可选链：单元测试以纯 Node 加载 `features/*` 的纯逻辑，
 * 那些模块 import 本文件，而纯 Node 下 `import.meta.env` 不存在。
 * Vite 构建把 `import.meta.env` 静态替换为对象，可选链不改变结果。
 * `SHOWCASE` 由 `lib/session` 转出，历史读者不改。
 * `READ_ONLY_REASON` 是展示构建下所有写入口的置灰原因，界面必须可见。
 * 正常构建下 `SHOWCASE` 折叠为 `false`，各分支被 tree-shake。
 */

/** 展示构建判定。全等 `'1'`，不接受其他真值。 */
export const SHOWCASE: boolean = import.meta.env?.VITE_SHOWCASE === '1'

/** 展示构建的只读原因。所有写入口共用这一句。 */
export const READ_ONLY_REASON = '展示模式只读'
