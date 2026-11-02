package schedule

// 看板签到统计的取值对象。
//
// # 功能
// 定义按日、按状态的账号终态计数。本文件**不**查库、**不**算窗口。
//
// # 归属
// core 层纯取值包。app/schedule 与 adapters/pg 共用；
// 适配器因此不必导入 app。
//
// # 口径
// 日桶互斥：每个账号每日恰落一个状态。
// ByStatus 是窗口内按状态去重的账号数，桶间可有交集，不得相加当 Total。
// Days 只含库里出现过的日期；缺失日期由用例层补齐并列入 DaysMissing。

// SignStatDay 是一天的账号终态分桶。
type SignStatDay struct {
	Day     string
	Pending int
	Running int
	Success int
	Failed  int
	Skipped int
}

// SignStatCounts 是窗口内按状态去重的账号数。
type SignStatCounts struct {
	Pending int
	Running int
	Success int
	Failed  int
	Skipped int
}

// SignStats 是窗口内的签到统计原始值。
type SignStats struct {
	Days     []SignStatDay
	Total    int
	ByStatus SignStatCounts
}
