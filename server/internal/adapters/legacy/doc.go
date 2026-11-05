// Package legacy 实现旧栈（Python/SQLite）导出物 → 新栈（Go/PostgreSQL）的一次性导入。
//
// # 功能
// 读导出器（scripts/db_export.py）产出的 JSONL 目录，
// 重建 users 与 accounts（含凭据重加密），并产出可逐行比对的
// reconciliation.jsonl 与 summary.json。
// 另含一次性补录：把旧 time_prefs（按 phone）写成新 time_prefs（按 user_id）。
// 本包只服务 `cmd/cli migrate-legacy` 与 `cmd/cli backfill-time-prefs`；
// 一次性迁移件，**不**进服务装配。补录的映射与跳过口径见 backfill.go。
//
// # 契约
//   - accounts 行的 password/phone_code 是空串或旧信封对象
//     （v/kid/nonce/ct/tag 十六进制，AES-256-GCM，AAD＝手机号）。
//   - 对账哈希：手机号先掩码（core/mask.MaskPhone 同一实现）后进
//     sha256，规范串与序列化口径见 canonical.go。
//   - legacy_ids 表是幂等判据：第二遍导入 0 新增。
//   - 事务契约：全部写、行级单元与
//     对账比对同在一个事务内。对账比对在提交前执行，不一致即回滚，
//     exit 2 零写入由事务保证。行级单元＝主行＋legacy_ids 映射＋凭据，
//     经 pg.DB.InSavepoint 同存亡：单元失败整行回滚，不留无映射的半行。
//     「账号照建」仅指凭据缺行/解密失败，仍照建。
//   - 行级错误文本只出掩码手机号（core/mask.MaskPhone），零明文；
//     输出走 Options.Err 注入端，不经全局 os.Stderr。
//
// # 裁决
//   - 旧库无 schools 表：不造学校，全部账号挂 --school-id（须已存在）。
//   - 旧 accounts.owner 逐行解析：先 lower(trim(email)) 精确命中旧 user，
//     再按邮箱 @ 前缀命中；同一级命中多行即行错误，不按行序猜。
//     旧 user → 新 user 走 legacy_ids（用户先导入，同事务可见），每条账号
//     写自己的 owner_user_id。对不上的 active 账号落
//     --fallback-owner-user-id（本批是旧内置 master 名下的账号）；
//     非 active 账号对不上时按行错误处理。
//   - 旧角色：admin→admin、user→user。旧 admin 额外写一条
//     admin_schools 绑定（学校取 --admin-school-id，缺省同 --school-id），
//     重跑 ON CONFLICT DO NOTHING 幂等。旧库的内置 master 不在 users 表，
//     部署时用新邮箱与新口令单独创建；导入器不导入、不伪造该身份。
//   - 旧 accounts.name 写入新 accounts.name（空串照常）；旧 sort_order
//     在新 schema 无对应列，弃置并在 summary 声明。
//   - 旧 time_prefs 挂手机号（每账号一行），新栈 time_prefs 挂 user_id
//     （每用户一行、作用于名下全部账号）：导入器不导入、显性计数。
//     值语义本身等价（旧 slot_min＝相对窗口起点的分钟偏移、5 对齐＝新
//     offset_min），缺的是挂接点，不是数值口径。
//     挂接点由 `cmd/cli backfill-time-prefs` 在导入后补齐：
//     它沿 legacy_ids 找新账号的 owner_user_id，按用户写一条。
//   - 旧 audit_logs 不写入新 audit_logs：append-only 链不可跨链续。
//   - 明文驻留行：账号照建、凭据行缺省、显性计数。
//   - 新负载三键 device_code/password/phone_model 全部出现；
//     device_code 置空串（解码侧允许空串，app/account/cipher.go
//     DisallowUnknownFields 只拒缺键与未知键，2026-10-27 已核）。
//     旧 phone_code（验证码信封）在新负载无对应键，弃置并显性计数。
//   - 日志与报告不回放完整邮箱、姓名、手机号或 owner 原文；
//     行级错误只带旧行 id 与掩码手机号，summary 只出计数。
//
// # 归属
// adapters 层一次性迁移件。SQL 直写（仓储端口表达不了状态回填、
// deleted_at 与 created_at 迁移语义）。凭据封装走 Sealer 缝：实现在是
// app/account 的 Cipher，由 cmd/cli 装配注入——boundary 门禁止 adapters
// 导入 app，本包不重写 AES-GCM。
package legacy
