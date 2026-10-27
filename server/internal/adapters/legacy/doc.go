// Package legacy 实现旧栈（Python/SQLite）导出物 → 新栈（Go/PostgreSQL）的一次性导入。
//
// # 功能
// 读 P8-A 导出器（scripts/db_export.py）产出的 JSONL 目录，
// 重建 users 与 accounts（含凭据重加密），并产出可逐行比对的
// reconciliation.jsonl 与 summary.json。
// 本包只服务 `cmd/cli migrate-legacy`；一次性迁移件，**不**进服务装配。
//
// # 契约（与 P8-A 导出器钉死，2026-10-27 派单稿）
//   - accounts 行的 password/phone_code 是空串或旧信封对象
//     （v/kid/nonce/ct/tag 十六进制，AES-256-GCM，AAD＝手机号）。
//   - 对账哈希：手机号先掩码（core/mask.MaskPhone 同一实现）后进
//     sha256，规范串与序列化口径见 canonical.go。
//   - legacy_ids 表是幂等判据：第二遍导入 0 新增。
//
// # 裁决（派单稿「裁决补遗」）
//   - 旧库无 schools 表：不造学校，全部账号挂 --school-id（须已存在）。
//   - 旧 accounts.owner 是自由文本：不做可信映射，全部账号挂
//     --owner-user-id，旧 owner 值分布进 summary。
//   - 旧 time_prefs 挂手机号（每账号一行），新栈 time_prefs 挂 user_id
//     （每用户一行、作用于名下全部账号）：导入器拿不到可信的
//     phone→user 映射，硬塞会让多行塌缩互相覆盖，故**不导入**、显性计数。
//     值语义本身等价（旧 slot_min＝相对窗口起点的分钟偏移、5 对齐＝新
//     offset_min），缺的是挂接点，不是数值口径。
//   - 旧 audit_logs 不写入新 audit_logs：append-only 链不可跨链续。
//   - 明文驻留行：账号照建、凭据行缺省、显性计数。
//   - 新负载三键 device_code/password/phone_model 全部出现；
//     device_code 置空串（解码侧允许空串，app/account/cipher.go
//     DisallowUnknownFields 只拒缺键与未知键，2026-10-27 已核）。
//     旧 phone_code（验证码信封）在新负载无对应键，弃置并显性计数。
//
// # 归属
// adapters 层一次性迁移件。SQL 直写（仓储端口表达不了状态回填、
// deleted_at 与 created_at 迁移语义）。凭据封装走 Sealer 缝：实现在是
// app/account 的 Cipher，由 cmd/cli 装配注入——boundary 门禁止 adapters
// 导入 app，本包不重写 AES-GCM。
package legacy
