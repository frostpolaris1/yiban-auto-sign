/**
 * **功能**
 * 程序版本号的唯一读取点。
 *
 * **归属**
 * `lib` 工具层：只读构建期常量，不发请求。
 *
 * **口径**
 * `__APP_VERSION__` 由 `vite.config.ts` 从 `package.json` 注入。
 * 壳页脚与认证页页脚共用；发版只改 `package.json`。
 */

export const APP_VERSION = __APP_VERSION__
