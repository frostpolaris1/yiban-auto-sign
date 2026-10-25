# web/static/vue —— Vue 构建产物

## 放什么

由 `frontend/` 源码构建出的产物：每页一个 HTML、`assets/` 下的脚本与样式。
本目录的文件是构建结果，不是源码。

## 不放什么

不放源码（在 `frontend/src/`）、不放手工修改、不放依赖包。
要改行为，改 `frontend/src/` 后重新构建。

## 入口

构建：在 `frontend/` 执行 `npm run build`（vite，`outDir` 指向本目录）。
产物与源码同源：`scripts/check-vue-build-reproducible.sh`。

## 遗留与删除条件

目录名保留原样：29 个文件提及 `web/static/vue/`。旧页只减不增。
删除条件：旧模板页全部退役、只剩 Vue 产物时，重新评估目录名。
