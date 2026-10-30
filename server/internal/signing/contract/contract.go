// Package contract 定义宿主框架与平台提供方之间的 v1 固定接口。
//
// # 功能
// 定义窗口与目标发现、凭据校验、签到尝试、结果词表与能力声明。
// 提供方**不得**导入宿主、**不得**碰存储、**不得**记录凭据。
// 本包**不**发请求、**不**存数据、**不**做调度。
//
// # 归属
// 宿主与平台适配器之间的唯一接缝。
// 平台实现负责协议；宿主负责图片预处理与重试决策。
//
// # 复用
// Provider；DiscoverRequest、VerifyRequest、SignRequest；
// Window、Target、Location、ImageRef；
// Profile、Result、Evidence、RetryAdvice、Capabilities；
// Outcome 及全部结果常量；OutcomeError；Credential、AccountRef。
//
// # 口径
// Outcome 是中性词表；unknown 与 success **必须保持区分**。
// 发现与校验的结论错误用 OutcomeError 表达；Outcome 必须在词表内，
// 且不得为 success。宿主用 errors.As 取回，不认提供方私有类型。
// Credential 只在一次调用内有效，提供方不得留存或记录。
package contract

import (
	"context"
	"time"
)

// Provider 是唯一的平台相关接缝。
//
// 能力与调用约定：
// 未声明的发现能力，对应方法**必须返回非 nil 错误**；宿主不得调用该方法。
// 宿主只按 Capabilities 决定 UI 与调度，不假设提供方的平台细节。
type Provider interface {
	ID() string
	Capabilities() Capabilities
	DiscoverWindow(ctx context.Context, req DiscoverRequest) (Window, error)
	DiscoverTargets(ctx context.Context, req DiscoverRequest) ([]Target, error)
	Verify(ctx context.Context, req VerifyRequest) (Profile, error)
	Sign(ctx context.Context, req SignRequest) (Result, error)
}

// AccountRef 是宿主持有的账号标识。
type AccountRef struct {
	ID       string
	SchoolID string
	Platform string
}

// Credential 只在一次调用内有效。
// **提供方不得留存或记录它**。
type Credential struct {
	Phone      string
	Password   string
	DeviceCode string
	PhoneModel string
}

// WindowSource 记录窗口的来源。
type WindowSource string

const (
	WindowPlatform WindowSource = "platform"
	// WindowCache 是历史缓存行的来源词。
	// 运行中的服务不写它：新发现标 platform，回退标 fallback。
	// 写入者只有迁移 00014 的 Down 段（把 override 行降级标为 cache）；
	// 读面按行原样透传，故词表保留此值。
	WindowCache    WindowSource = "cache"
	WindowFallback WindowSource = "fallback"
	// WindowOverride 是 master 或 admin 对某任务某日的人工覆盖。
	// 优先级最高：覆盖存在时不再采信平台窗口。
	WindowOverride WindowSource = "override"
)

// Window 是一个签到窗口。
type Window struct {
	Start  time.Time
	End    time.Time
	Source WindowSource
	Raw    map[string]any
}

// Point 是 WGS84 坐标。
type Point struct {
	Lng float64
	Lat float64
}

// Target 是一个可签到地点。
type Target struct {
	ID             string
	Title          string
	Address        string
	Polygon        []Point
	Center         Point
	RadiusM        int
	RequiresPhoto  bool
	AttachmentName string
	Raw            map[string]any
}

// LocationMode 选择自动采样还是固定点。
type LocationMode string

const (
	LocationAuto  LocationMode = "auto"
	LocationFixed LocationMode = "fixed"
)

// Location 是宿主对一次尝试的定位策略。
type Location struct {
	Mode   LocationMode
	Lng    float64
	Lat    float64
	Reason string
}

// ImageRef 是已预处理好的图片。
// 宿主先完成旋转、裁剪与重编码；提供方只负责上传与提交。
type ImageRef struct {
	ID     string
	Mime   string
	Width  int
	Height int
	SHA256 string
	Bytes  []byte
}

// DiscoverRequest 向提供方索取一个账号的窗口或目标。
type DiscoverRequest struct {
	Account    AccountRef
	Credential Credential
}

// VerifyRequest 请提供方校验一组凭据。
type VerifyRequest struct {
	Account    AccountRef
	Credential Credential
}

// Profile 是提供方可以返回的非秘密身份信息。
type Profile struct {
	DisplayName string
	SchoolName  string
	StudentID   string
	Raw         map[string]any
}

// SignRequest 是一次签到尝试。
type SignRequest struct {
	AttemptID  string
	Account    AccountRef
	Credential Credential
	Window     Window
	Target     Target
	Location   Location
	Image      *ImageRef
	Options    map[string]string
}

// Outcome 是中性结果词表。
type Outcome string

const (
	OutcomeSuccess         Outcome = "success"
	OutcomeAlreadyDone     Outcome = "already_done"
	OutcomePendingUpstream Outcome = "pending_upstream"
	OutcomeNoTarget        Outcome = "no_target"
	OutcomeInvalidTarget   Outcome = "invalid_target"
	OutcomeCredentialBad   Outcome = "credential_invalid"
	OutcomeDeviceUnbound   Outcome = "device_unbound"
	OutcomeChallenge       Outcome = "challenge"
	OutcomeBlocked         Outcome = "blocked"
	OutcomeTransient       Outcome = "transient"
	OutcomeRejected        Outcome = "rejected"
	OutcomePhotoMissing    Outcome = "photo_missing"
	OutcomePhotoRejected   Outcome = "photo_rejected"
	OutcomeUnknown         Outcome = "unknown"
)

// Valid 报告 o 是否为已知结果。
func (o Outcome) Valid() bool {
	switch o {
	case OutcomeSuccess, OutcomeAlreadyDone, OutcomePendingUpstream,
		OutcomeNoTarget, OutcomeInvalidTarget, OutcomeCredentialBad,
		OutcomeDeviceUnbound, OutcomeChallenge, OutcomeBlocked, OutcomeTransient,
		OutcomeRejected, OutcomePhotoMissing, OutcomePhotoRejected,
		OutcomeUnknown:
		return true
	default:
		return false
	}
}

// OutcomeError 把发现或校验的结论放进 error。
//
// 用途：DiscoverWindow、DiscoverTargets 与 Verify 只返回 (值, error)。
// 平台给出「已签到」「补签中」「凭据错」这类结论时，提供方返回
// &OutcomeError{Outcome: ..., Message: ...}，宿主用 errors.As 取回。
// Outcome 必须在词表内，且不得为 success；Message 不得含凭据原值。
// 取消与网络错误继续用原生 error，不要包成 OutcomeError。
type OutcomeError struct {
	Outcome Outcome
	Message string
}

func (e *OutcomeError) Error() string {
	if e.Message != "" {
		return e.Message
	}
	return "provider: " + string(e.Outcome)
}

// Evidence 是审计安全的结果明细。Raw **必须已经掩码**。
type Evidence struct {
	Rank        *int
	SignedAt    *time.Time
	Receipt     string
	PhotoID     string
	PhotoSHA256 string
	Raw         map[string]any
}

// RetryAdvice 告诉宿主是否重试、何时重试。
type RetryAdvice struct {
	Retryable    bool
	After        time.Duration
	MaxAttempts  int
	ClearSession bool
}

// Result 是一次尝试的结果。
type Result struct {
	Outcome  Outcome
	Evidence Evidence
	Retry    RetryAdvice
	Message  string
}

// Capabilities 让宿主适配界面与调度。
type Capabilities struct {
	WindowDiscovery bool
	TargetDiscovery bool
	ImageRequired   bool
	ManualCaptcha   bool
	DeviceBinding   bool
	FixedLocation   bool
	Rank            bool
}
