package conformance

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"reflect"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// 判据名称。测试与报告都按这些字符串定位。
const (
	CheckConstructible  = "provider.constructible"
	CheckIDNonEmpty     = "id.nonempty"
	CheckIDStable       = "id.stable"
	CheckCapsStable     = "capabilities.stable"
	CheckCapsHonored    = "capabilities.honored"
	CheckWindowShape    = "window.shape"
	CheckWindowCancel   = "window.cancel"
	CheckTargetsShape   = "targets.shape"
	CheckTargetsCancel  = "targets.cancel"
	CheckVerifyResult   = "verify.result"
	CheckSignResult     = "sign.result"
	CheckPIIAbsent      = "pii.absent"
	CheckSignRetry      = "sign.retry"
	CheckSignUnknown    = "sign.unknown"
	CheckSignCancel     = "sign.cancel"
	CheckSignIdempotent = "sign.idempotent"
	CheckSignConcurrent = "sign.concurrent"
)

const (
	// signSamples 与契约结果数量一致，使一次采样覆盖全部结果。
	signSamples = 13
	// concurrentWorkers 是并发判据的协程数。
	concurrentWorkers = 8
	// concurrentTimeout 是并发判据的完成上限。
	concurrentTimeout = 10 * time.Second
)

// NewProvider 构造一个全新的提供方实例。
// **每条判据都重新构造**，判据之间不共享状态。
type NewProvider func() contract.Provider

// Expecter 由需要声明期望结果的提供方实现。
// 返回 false 表示该请求没有确定期望。
type Expecter interface {
	ExpectedOutcome(req contract.SignRequest) (contract.Outcome, bool)
}

// Check 是一条判据。
// Run 返回 nil 表示通过；否则返回统一格式的失败原因。
type Check struct {
	Name string
	Run  func(NewProvider) error
}

// Checks 返回全部判据，顺序固定。
func Checks() []Check {
	return []Check{
		{CheckConstructible, checkConstructible},
		{CheckIDNonEmpty, checkIDNonEmpty},
		{CheckIDStable, checkIDStable},
		{CheckCapsStable, checkCapsStable},
		{CheckCapsHonored, checkCapsHonored},
		{CheckWindowShape, checkWindowShape},
		{CheckWindowCancel, checkWindowCancel},
		{CheckTargetsShape, checkTargetsShape},
		{CheckTargetsCancel, checkTargetsCancel},
		{CheckVerifyResult, checkVerifyResult},
		{CheckSignResult, checkSignResult},
		{CheckPIIAbsent, checkPIIAbsent},
		{CheckSignRetry, checkSignRetry},
		{CheckSignUnknown, checkSignUnknown},
		{CheckSignCancel, checkSignCancel},
		{CheckSignIdempotent, checkSignIdempotent},
		{CheckSignConcurrent, checkSignConcurrent},
	}
}

// Run 逐条执行判据。
// 失败文本统一为 `conformance: <名称>: <原因>`。
func Run(t *testing.T, newProvider NewProvider) {
	t.Helper()
	for _, check := range Checks() {
		check := check
		t.Run(check.Name, func(t *testing.T) {
			t.Helper()
			if err := runCheck(check, newProvider); err != nil {
				t.Error(err)
			}
		})
	}
}

// runCheck 执行一条判据，并把 panic 转成同格式失败。
// 提供方 panic 不得拖垮整轮判据。
func runCheck(check Check, newProvider NewProvider) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = failf(check.Name, "panic: %v", r)
		}
	}()
	return check.Run(newProvider)
}

// failf 生成统一失败文本。
func failf(name, format string, args ...any) error {
	return fmt.Errorf("conformance: %s: %s", name, fmt.Sprintf(format, args...))
}

// checkConstructible 要求构造器返回可用的对象。
func checkConstructible(newProvider NewProvider) error {
	p := newProvider()
	if p == nil {
		return failf(CheckConstructible, "构造器返回 nil")
	}
	value := reflect.ValueOf(p)
	switch value.Kind() {
	case reflect.Ptr, reflect.Interface, reflect.Map, reflect.Slice, reflect.Func, reflect.Chan:
		if value.IsNil() {
			return failf(CheckConstructible, "构造器返回类型化 nil（%T）", p)
		}
	}
	return nil
}

// checkIDNonEmpty 要求 ID 非空。
func checkIDNonEmpty(newProvider NewProvider) error {
	if strings.TrimSpace(newProvider().ID()) == "" {
		return failf(CheckIDNonEmpty, "ID() 为空")
	}
	return nil
}

// checkIDStable 要求 ID 在多次调用间不变。
func checkIDStable(newProvider NewProvider) error {
	p := newProvider()
	first := p.ID()
	for i := 0; i < 2; i++ {
		if got := p.ID(); got != first {
			return failf(CheckIDStable, "第 %d 次 ID() = %q，首值 %q", i+2, got, first)
		}
	}
	return nil
}

// checkCapsStable 要求能力声明在多次调用间不变。
func checkCapsStable(newProvider NewProvider) error {
	p := newProvider()
	first := p.Capabilities()
	if got := p.Capabilities(); got != first {
		return failf(CheckCapsStable, "两次能力不同：%+v 与 %+v", first, got)
	}
	return nil
}

// checkCapsHonored 要求未声明能力的方法返回错误。
// 静默零值会被宿主当成一次成功的发现（缺口 G1）。
func checkCapsHonored(newProvider NewProvider) error {
	p := newProvider()
	caps := p.Capabilities()
	ctx := context.Background()

	if !caps.WindowDiscovery {
		if _, err := p.DiscoverWindow(ctx, discoverRequest()); err == nil {
			return failf(CheckCapsHonored, "未声明 WindowDiscovery，DiscoverWindow 却返回 nil 错误")
		}
	}
	if !caps.TargetDiscovery {
		if _, err := p.DiscoverTargets(ctx, discoverRequest()); err == nil {
			return failf(CheckCapsHonored, "未声明 TargetDiscovery，DiscoverTargets 却返回 nil 错误")
		}
	}
	return nil
}

// checkWindowShape 要求窗口符合契约形状。
func checkWindowShape(newProvider NewProvider) error {
	p := newProvider()
	if !p.Capabilities().WindowDiscovery {
		return nil
	}
	window, err := p.DiscoverWindow(context.Background(), discoverRequest())
	if err != nil {
		return failf(CheckWindowShape, "DiscoverWindow 返回错误: %v", err)
	}
	if window.Start.IsZero() || window.End.IsZero() {
		return failf(CheckWindowShape, "窗口起止含零值：%s..%s", window.Start, window.End)
	}
	if !window.End.After(window.Start) {
		return failf(CheckWindowShape, "窗口结束 %s 不晚于开始 %s", window.End, window.Start)
	}
	switch window.Source {
	case contract.WindowPlatform, contract.WindowCache, contract.WindowFallback:
	default:
		return failf(CheckWindowShape, "窗口来源 %q 不在契约词表内", window.Source)
	}
	return nil
}

// checkWindowCancel 要求已取消上下文返回 context.Canceled。
func checkWindowCancel(newProvider NewProvider) error {
	p := newProvider()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := p.DiscoverWindow(ctx, discoverRequest()); !errors.Is(err, context.Canceled) {
		return failf(CheckWindowCancel, "错误 = %v，want context.Canceled", err)
	}
	return nil
}

// checkTargetsShape 要求目标 ID 与坐标合法。
func checkTargetsShape(newProvider NewProvider) error {
	p := newProvider()
	if !p.Capabilities().TargetDiscovery {
		return nil
	}
	targets, err := p.DiscoverTargets(context.Background(), discoverRequest())
	if err != nil {
		return failf(CheckTargetsShape, "DiscoverTargets 返回错误: %v", err)
	}
	for i, target := range targets {
		if strings.TrimSpace(target.ID) == "" {
			return failf(CheckTargetsShape, "第 %d 个目标缺少 ID", i+1)
		}
		if target.RadiusM < 0 {
			return failf(CheckTargetsShape, "第 %d 个目标半径为负数: %d", i+1, target.RadiusM)
		}
		points := make([]contract.Point, 0, len(target.Polygon)+1)
		points = append(points, target.Polygon...)
		points = append(points, target.Center)
		for _, point := range points {
			if err := validPoint(point); err != nil {
				return failf(CheckTargetsShape, "第 %d 个目标坐标无效: %v", i+1, err)
			}
		}
	}
	return nil
}

// checkTargetsCancel 要求已取消上下文返回 context.Canceled。
func checkTargetsCancel(newProvider NewProvider) error {
	p := newProvider()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := p.DiscoverTargets(ctx, discoverRequest()); !errors.Is(err, context.Canceled) {
		return failf(CheckTargetsCancel, "错误 = %v，want context.Canceled", err)
	}
	return nil
}

// checkVerifyResult 要求正常路径的校验返回 nil 错误。
func checkVerifyResult(newProvider NewProvider) error {
	if _, err := newProvider().Verify(context.Background(), verifyRequest()); err != nil {
		return failf(CheckVerifyResult, "Verify 返回错误: %v", err)
	}
	return nil
}

// checkSignResult 要求结果在契约词表内。
func checkSignResult(newProvider NewProvider) error {
	results, err := sampleSign(newProvider(), "attempt-result", signSamples)
	if err != nil {
		return failf(CheckSignResult, "%v", err)
	}
	for i, result := range results {
		if !result.Outcome.Valid() {
			return failf(CheckSignResult, "第 %d 次结果 %q 不在契约词表内", i+1, result.Outcome)
		}
	}
	return nil
}

// checkPIIAbsent 要求身份与结果不含凭据原值，也不含明文手机号。
func checkPIIAbsent(newProvider NewProvider) error {
	p := newProvider()
	cred := testCredential()

	if profile, err := p.Verify(context.Background(), verifyRequest()); err == nil {
		if leak := findLeak(profile, cred); leak != "" {
			return failf(CheckPIIAbsent, "身份信息泄漏%s", leak)
		}
	}
	results, err := sampleSign(p, "attempt-pii", signSamples)
	if err != nil {
		return failf(CheckPIIAbsent, "%v", err)
	}
	for i, result := range results {
		if leak := findLeak(result, cred); leak != "" {
			return failf(CheckPIIAbsent, "第 %d 次签到结果泄漏%s", i+1, leak)
		}
	}
	return nil
}

// checkSignRetry 要求重试分级与契约结果表一致。
func checkSignRetry(newProvider NewProvider) error {
	results, err := sampleSign(newProvider(), "attempt-retry", signSamples)
	if err != nil {
		return failf(CheckSignRetry, "%v", err)
	}
	for i, result := range results {
		if !result.Outcome.Valid() {
			continue // sign.result 负责报告词表越界。
		}
		switch result.Outcome {
		case contract.OutcomeTransient:
			if !result.Retry.Retryable {
				return failf(CheckSignRetry, "第 %d 次 transient 必须可重试", i+1)
			}
			if result.Retry.MaxAttempts < 1 {
				return failf(CheckSignRetry, "第 %d 次 transient 可重试但 MaxAttempts 为 0", i+1)
			}
		case contract.OutcomeChallenge:
			if result.Retry.Retryable && result.Retry.MaxAttempts < 1 {
				return failf(CheckSignRetry, "第 %d 次 challenge 可重试但 MaxAttempts 为 0", i+1)
			}
		default:
			if result.Retry.Retryable {
				return failf(CheckSignRetry, "第 %d 次 %s 不可自动重试，却标记可重试", i+1, result.Outcome)
			}
		}
	}
	return nil
}

// checkSignUnknown 要求 unknown 永不自动重试。
func checkSignUnknown(newProvider NewProvider) error {
	results, err := sampleSign(newProvider(), "attempt-unknown", signSamples)
	if err != nil {
		return failf(CheckSignUnknown, "%v", err)
	}
	for i, result := range results {
		if result.Outcome == contract.OutcomeUnknown && result.Retry.Retryable {
			return failf(CheckSignUnknown, "第 %d 次 unknown 被标记可重试", i+1)
		}
	}
	return nil
}

// checkSignCancel 要求取消后不报告成功。
func checkSignCancel(newProvider NewProvider) error {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	result, err := newProvider().Sign(ctx, signRequest("attempt-cancel"))
	if !errors.Is(err, context.Canceled) {
		return failf(CheckSignCancel, "错误 = %v，want context.Canceled", err)
	}
	if result.Outcome == contract.OutcomeSuccess {
		return failf(CheckSignCancel, "已取消上下文仍报告 %q", result.Outcome)
	}
	return nil
}

// checkSignIdempotent 要求同一 AttemptID 重复签到得到同一结果。
func checkSignIdempotent(newProvider NewProvider) error {
	p := newProvider()
	request := signRequest("attempt-idempotent")
	ctx := context.Background()

	first, err := p.Sign(ctx, request)
	if err != nil {
		return failf(CheckSignIdempotent, "首次 Sign 返回错误: %v", err)
	}
	second, err := p.Sign(ctx, request)
	if err != nil {
		return failf(CheckSignIdempotent, "重复 Sign 返回错误: %v", err)
	}
	if first.Outcome != second.Outcome {
		return failf(CheckSignIdempotent, "同一 AttemptID 得到 %q 与 %q", first.Outcome, second.Outcome)
	}
	return nil
}

// checkSignConcurrent 要求并发签到互不串扰。
// 实现 Expecter 时逐条比对期望；否则只覆盖词表与完成时限。
func checkSignConcurrent(newProvider NewProvider) error {
	p := newProvider()
	expecter, hasExpecter := p.(Expecter)

	requests := make([]contract.SignRequest, concurrentWorkers)
	results := make([]contract.Result, concurrentWorkers)
	errs := make([]error, concurrentWorkers)

	var wg sync.WaitGroup
	wg.Add(concurrentWorkers)
	for i := 0; i < concurrentWorkers; i++ {
		i := i
		requests[i] = signRequest(fmt.Sprintf("attempt-concurrent-%d", i))
		go func() {
			defer wg.Done()
			// 协程内 panic 无法被 runCheck 捕获；
			// 在这里转成错误，避免整个测试进程退出。
			defer func() {
				if r := recover(); r != nil {
					errs[i] = fmt.Errorf("panic: %v", r)
				}
			}()
			results[i], errs[i] = p.Sign(context.Background(), requests[i])
		}()
	}

	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(concurrentTimeout):
		return failf(CheckSignConcurrent, "%d 个并发 Sign 在 %s 内未返回", concurrentWorkers, concurrentTimeout)
	}

	for i := 0; i < concurrentWorkers; i++ {
		if errs[i] != nil {
			return failf(CheckSignConcurrent, "第 %d 个并发 Sign 返回错误: %v", i+1, errs[i])
		}
		if !results[i].Outcome.Valid() {
			return failf(CheckSignConcurrent, "第 %d 个并发结果 %q 不在契约词表内", i+1, results[i].Outcome)
		}
		if !hasExpecter {
			continue
		}
		want, declared := expecter.ExpectedOutcome(requests[i])
		if declared && results[i].Outcome != want {
			return failf(CheckSignConcurrent, "第 %d 个并发结果串扰：得到 %q，期望 %q", i+1, results[i].Outcome, want)
		}
	}
	return nil
}

// discoverRequest 返回判据用的固定发现请求。
func discoverRequest() contract.DiscoverRequest {
	return contract.DiscoverRequest{
		Account:    baseAccount(),
		Credential: testCredential(),
	}
}

// verifyRequest 返回判据用的固定校验请求。
func verifyRequest() contract.VerifyRequest {
	return contract.VerifyRequest{
		Account:    baseAccount(),
		Credential: testCredential(),
	}
}

// signRequest 返回判据用的固定签到请求。
// 图片、窗口、位置都是常量，使同一 AttemptID 的输入完全一致。
func signRequest(attemptID string) contract.SignRequest {
	return contract.SignRequest{
		AttemptID:  attemptID,
		Account:    baseAccount(),
		Credential: testCredential(),
		Window: contract.Window{
			Start:  baseTime,
			End:    baseTime.Add(30 * time.Minute),
			Source: contract.WindowPlatform,
		},
		Target: contract.Target{
			ID:      "target-conformance",
			Title:   "判据目标",
			Center:  contract.Point{Lng: 121.4705, Lat: 31.2305},
			RadiusM: 100,
		},
		Location: contract.Location{Mode: contract.LocationFixed, Lng: 121.4705, Lat: 31.2305},
		Image: &contract.ImageRef{
			ID:     "image-conformance",
			Mime:   "image/jpeg",
			Width:  640,
			Height: 480,
			SHA256: "0000000000000000000000000000000000000000000000000000000000000000",
			Bytes:  []byte{0xff, 0xd8, 0xff},
		},
	}
}

// baseTime 是判据唯一的时间点，避免读墙上时钟。
var baseTime = time.Date(2026, 10, 26, 6, 0, 0, 0, time.UTC)

// baseAccount 是判据用的固定账号。
func baseAccount() contract.AccountRef {
	return contract.AccountRef{ID: "acct-conformance", SchoolID: "school-conformance", Platform: "conformance"}
}

// testCredential 是判据用的固定凭据。
// **它是虚构值**；判据用它检查结果是否泄漏凭据。
func testCredential() contract.Credential {
	return contract.Credential{
		Phone:      "13800138000",
		Password:   "Passw0rd-conformance",
		DeviceCode: "device-conformance",
		PhoneModel: "model-conformance",
	}
}

// sampleSign 连续发起 n 次签到，AttemptID 互不相同。
// **不同 AttemptID 才不会被幂等缓存折叠**。
func sampleSign(p contract.Provider, prefix string, n int) ([]contract.Result, error) {
	results := make([]contract.Result, 0, n)
	for i := 0; i < n; i++ {
		result, err := p.Sign(context.Background(), signRequest(fmt.Sprintf("%s-%d", prefix, i)))
		if err != nil {
			return nil, fmt.Errorf("第 %d 次 Sign 返回错误: %w", i+1, err)
		}
		results = append(results, result)
	}
	return results, nil
}

// phoneLike 匹配未脱敏的 11 位手机号。
var phoneLike = regexp.MustCompile(`(^|[^0-9])1[3-9][0-9]{9}([^0-9]|$)`)

// findLeak 返回泄漏描述；无泄漏返回空串。
// 它同时检查凭据原值与形如手机号的明文数字。
// 序列化失败也算泄漏：**证明不了没有，就不算通过**。
func findLeak(value any, cred contract.Credential) string {
	raw, err := json.Marshal(value)
	if err != nil {
		return "无法序列化，无法证明已脱敏：" + err.Error()
	}
	text := strings.ToLower(string(raw))
	secrets := []struct{ name, value string }{
		{"手机号", cred.Phone},
		{"口令", cred.Password},
		{"设备码", cred.DeviceCode},
		{"设备型号", cred.PhoneModel},
	}
	for _, secret := range secrets {
		if secret.value == "" {
			continue
		}
		if strings.Contains(text, strings.ToLower(secret.value)) {
			return secret.name
		}
	}
	if phoneLike.MatchString(string(raw)) {
		return "明文手机号"
	}
	return ""
}

// validPoint 检查经纬度的物理范围。
func validPoint(point contract.Point) error {
	if math.IsNaN(point.Lng) || math.IsInf(point.Lng, 0) || point.Lng < -180 || point.Lng > 180 {
		return fmt.Errorf("经度 %v 越界", point.Lng)
	}
	if math.IsNaN(point.Lat) || math.IsInf(point.Lat, 0) || point.Lat < -90 || point.Lat > 90 {
		return fmt.Errorf("纬度 %v 越界", point.Lat)
	}
	return nil
}
