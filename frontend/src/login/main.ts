import { createApp } from "vue";
import "element-plus/theme-chalk/dark/css-vars.css";
import "../styles/ep-theme.css";
import { syncThemeWithShell } from "../lib/theme";
import Login from "./Login.vue";

/* 登录页入口（`/login`，唯一在登录态之外渲染的页面）。

   顺序契约同其它页：先同步主题（EP 暗色变量在首次渲染前生效），再挂载。
   本页零 Element Plus 组件，但仍与其它页一致地引 EP 样式与主题桥（理由见
   src/calendar/main.ts：EP 的 CSS/JS 本来就随共享 vendor chunk 到达，
   省掉只会制造不一致与"日后加 EP 组件无样式"的坑）。

   协议/隐私正文**不经过本入口**：它是服务端渲染进页面的惰性 `<template id="doc-*">`，
   由组件 cloneNode 进 core.js 的模态（Vue 侧零 v-html）。 */

syncThemeWithShell();
createApp(Login).mount("#vue-login-app");
