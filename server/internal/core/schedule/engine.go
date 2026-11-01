package schedule

// 执行监控快照的数据形状（UX-5）。
//
// # 功能
// 定义单进程执行监控的读取形状：在飞行、到期计数、当日终态计数、
// 待重试行。只读：本文件没有任何写路径。
//
// # 归属
// core/schedule 纯领域包。端口在 app/schedule，实现为 pg.ScheduleRepo；
// 形状放在 core 与仓库先例一致（TaskQueryRow、WindowRecord 同款）：
// 适配器不 import app。
//
// # 口径
// EngineInflightRow.Phone 与 EngineRetryRow.Phone 是**明文**手机号，
// 只允许在 app 层的用例里出现，出用例前必须经 core/mask 掩码；
// 入口层与日志只见掩码值。
// Inflight 只收 running 且已写认领与租约的行：缺任一时刻的行没有
// 监控意义，也不该伪装成在飞。
// Retrying 只收 pending 且 attempts>0 的行：attempts 计业务尝试，
// 等待型重排不计（RequeueWait 口径），故等待中的行不出现。
// DueNow 计 pending 且 run_at<=now 的行，按库内落点，不再判账号
// 可用性——可用性是认领判据，监控数如实反映「还有多少行到点未执行」。

import "time"

// EngineInflightRow 是一行在飞任务。Phone 是明文：出用例前必须掩码。
type EngineInflightRow struct {
	AccountID      string
	Phone          string
	SchoolID       string
	SchoolName     string
	ClaimedAt      time.Time
	LeaseExpiresAt time.Time
}

// EngineRetryRow 是一行待重试任务。Phone 是明文：出用例前必须掩码。
type EngineRetryRow struct {
	AccountID  string
	Phone      string
	SchoolID   string
	SchoolName string
	Attempts   int
	RunAt      time.Time
	Result     string
}

// EngineData 是一次执行监控读取的产物。
// 计数与列表来自同一时刻的多次查询：单进程写方只有执行循环，
// 秒级窗口内的读偏移不改变监控语义。
type EngineData struct {
	Inflight     []EngineInflightRow
	DueNow       int64
	TodaySuccess int64
	TodayFailed  int64
	Retrying     []EngineRetryRow
}
