import { createApp } from "vue";
import { syncThemeWithShell } from "../lib/theme";
import Login from "./Login.vue";

/* 登录页入口（`/login`，唯一在登录态之外渲染的页面）。

   顺序契约同其它页：先同步主题（EP 暗色变量在首次渲染前生效），再挂载。
   本页零 Element Plus 组件，故**不引 EP 的 CSS/主题桥**——vendor chunk 拆分修复
   （见 vite.config.ts 的 codeSplitting 注释）后，本页依赖闭包不含 EP，不必再为 EP 付体积。

   协议/隐私正文**不经过本入口**：它是服务端渲染进页面的惰性 `<template id="doc-*">`，
   由组件 cloneNode 进 core.js 的模态（Vue 侧零 v-html）。 */

syncThemeWithShell();
createApp(Login).mount("#vue-login-app");
