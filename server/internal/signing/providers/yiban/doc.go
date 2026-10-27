// Package yiban 是易班平台的 HTTP 会话与登录链。
//
// # 功能
// 维护每账号的内存 cookie jar 与 CSRF，执行四跳登录链，
// 解析身份，并缓存会话供后续签到调用复用。
// 本包**不**做调度、**不**写库、**不**求解验证码、**不**伪造设备。
//
// # 归属
// signing/providers 下的平台适配层。协议纯函数在 signing/protocol；
// 本包只负责请求顺序、头部、cookie 与错误分类。
//
// # 复用
// Client、Config、Endpoints、DefaultEndpoints；Credential 来自 contract。
//
// # 口径
// 登录链固定四跳：授权页 → usersure → iframe → 认证。
// usersure 必须不带 Origin/Referer，带了两者会得 e001；体以空 scope= 收尾。
// WAF/挑战判定复刻旧实现（waf.go）：形态判据不受长度限制，关键词判据只查
// 短响应；usersure 与认证每跳都先判 WAF 再解析。
// 认证成功采签发回执判据：code 成功且 data 非空且非 {Action,Data} 入口步；
// 身份字段只作展示面，缺失以空 Profile 成功。
// 会话按账号互斥；999 失效后重登一次；空闲 30 分钟过期。
// 网络错误保持原生 error；平台结论用 contract.OutcomeError。
// 挑战页只检测不求解，命中即返回 OutcomeChallenge。
// 消息一律经 core/mask 掩码，长度不超过 200 字符。
package yiban
