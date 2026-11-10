# 安全政策

本仓是个人维护的开源项目（AGPL-3.0）。
安全问题的报告请走**私密渠道**：不要在公开 issue、PR、Discussions 或截图里贴漏洞细节、利用代码与任何真实数据。

## 1. 适用范围

下列问题按安全漏洞处理：

- **凭据与会话**：口令、Cookie、会话令牌的泄露、越权复用，或明文落盘。
- **越权与权限提升**：跨用户读取或操作账号、管理员接口未鉴权、权限分级可绕过。
- **SSRF**：通知渠道、Webhook、图片抓取等出站请求可被诱导访问内网或云元数据端点。
- **脱敏失效**：手机号、邮箱、学校与人员标识在 API、页面、日志或导出中明文出现。
- **审计完整性**：审计链可伪造、可截断、可静默改写。
- **发布与部署面**：签名校验、升级脚本、备份恢复中的命令注入或凭据外泄。

平台风控规避类「功能请求」不算安全问题。
**验证码自动识别是本仓红线：不接受、不实现。**

## 2. 私密上报

使用 GitHub 的私密漏洞上报（Private Vulnerability Reporting，走 Security Advisories）：

1. 打开仓库 **Security** 页 → **Advisories** → **Report a vulnerability**；
2. 或直接访问 `https://github.com/frostpolaris1/yiban-auto-sign/security/advisories/new`。

如果页面上看不到 **Report a vulnerability** 入口，说明该功能尚未由维护者开启。
此时请开一个**不含任何漏洞细节**的公开 issue，只写「需要私密安全通道」；
维护者开启入口后，再在 advisory 里补齐内容。

本文件不提供邮箱或其他联系方式。
安全讨论只留在上述私密 advisory 内。

## 3. 报告里请写

- 受影响的版本或提交（release tag 或 `git rev-parse HEAD`）。
- 部署形态（裸机 / Docker / GitHub Actions）与复现前提。
- 复现步骤、期望结果、实际结果。
- **已脱敏**的证据：日志、请求、截图。手机号、邮箱、口令、URL token、设备码一律打码或改用合成值。
- 影响面判断：能读到什么、能改什么、是否需要登录。

未脱敏的报告会被直接删除，以免二次泄露。

## 4. 响应口径

- 本项目由个人维护，**尽力而为，无 SLA**：不承诺响应时限，也不承诺修复时限。
- 确认后优先在私密 advisory 内修复与协调披露；修复合入活跃线 `develop` 并随发布进入 `main` 后，再公开细节。
- 下列情况可能直接关闭：要求协助绕过平台风控、要求自动识别验证码、贴真实用户数据、只有扫描器输出而无复现。
- 修复发布后，如果你愿意，会在 advisory 与 `CHANGELOG.md` 里用你的 GitHub 账号署名。

## 5. 相关文档

- 数据处理与公开面纪律：[`PRIVACY_POLICY.md`](PRIVACY_POLICY.md)
- 贡献流程与不接受外部 PR 的面：[`CONTRIBUTING.md`](CONTRIBUTING.md)
