package app

// 本文件定义签名结论到日志级别的映射口径。
//
// 归属：app 层共用口径。worker 的结论日志与 schedule 的窗口发现日志同用。
//
// 口径：正常结论 Info；需人处置的结论 Warn。
// 一个学校 N 个已签账号每天产生 N 条 already_done；
// 若都给 Warn，按级别告警的运维面会被正常结论淹没。

import (
	"log/slog"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// OutcomeLogLevel 把一次签名结论映射成日志级别。
//
// Warn：transient、unknown、challenge、blocked、rejected、device_unbound、
// credential_invalid、photo_rejected——这些需要人看或告警。
// Info：success、already_done、no_target、invalid_target、
// photo_missing、pending_upstream——正常结论。
func OutcomeLogLevel(outcome contract.Outcome) slog.Level {
	switch outcome {
	case contract.OutcomeTransient, contract.OutcomeUnknown, contract.OutcomeChallenge,
		contract.OutcomeBlocked, contract.OutcomeRejected, contract.OutcomeDeviceUnbound,
		contract.OutcomeCredentialBad, contract.OutcomePhotoRejected:
		return slog.LevelWarn
	}
	return slog.LevelInfo
}
