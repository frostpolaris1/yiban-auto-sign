package assembly

// 窗口发现的多候选轮换单测。
//
// # 功能
// 钉住 scheduleDiscoverer 的候选轮换口径：一个坏号只让它自己那一轮失败。
// 判据：
//   1. 首号返回 credential_invalid 时换第二个号，只用第二个号的窗口；
//   2. 候选数与 limit 取自 discoveryCandidates（3），不按 created_at 取一条；
//   3. 急停（PausedError）与 ctx 取消立即返回，不再轮换，错误原样上抛；
//   4. 全部候选失败时探测 3 次并返回最后一个错误，不返回空窗口伪装成功；
//   5. 空候选保留 not_found 语义，不 panic；
//   6. provider 回非法窗口（端点早于起点）不算成功，继续下一个候选；
//   7. 失败日志只含标识与闭集分类，不含手机号、口令或邮箱。
//
// 这些断言都在假仓储/假 provider 上做：真实 provider 需要平台会话，
// 且我们只关心轮换决策，不关心 HTTP 细节。

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"testing"
	"time"

	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// fakeDiscoveryAccounts 是 discoveryAccounts 的假实现：候选与凭据都预先排好。
type fakeDiscoveryAccounts struct {
	candidates      []appaccount.Record
	candidatesErr   error
	lastSchoolID    string
	lastLimit       int
	credentials     map[string]appaccount.SigningCredential
	credentialErr   map[string]error
	credentialCalls []string
}

func (f *fakeDiscoveryAccounts) DiscoveryCandidatesForSchool(_ context.Context, schoolID string, limit int) ([]appaccount.Record, error) {
	f.lastSchoolID = schoolID
	f.lastLimit = limit
	if f.candidatesErr != nil {
		return nil, f.candidatesErr
	}
	return f.candidates, nil
}

func (f *fakeDiscoveryAccounts) SigningCredential(_ context.Context, _ corescope.Scope, id string) (appaccount.SigningCredential, error) {
	f.credentialCalls = append(f.credentialCalls, id)
	if err := f.credentialErr[id]; err != nil {
		return appaccount.SigningCredential{}, err
	}
	return f.credentials[id], nil
}

// fakeDiscoveryTasks 读一个固定的校级任务。
type fakeDiscoveryTasks struct {
	info coreschedule.TaskInfo
	err  error
}

func (f fakeDiscoveryTasks) Task(context.Context, string) (coreschedule.TaskInfo, error) {
	return f.info, f.err
}

// providerResult 是假 provider 第 n 次调用的结果。
type providerResult struct {
	window contract.Window
	err    error
}

// fakeWindowProvider 按调用顺序回放结果，并记录每次请求。
type fakeWindowProvider struct {
	results []providerResult
	calls   []contract.DiscoverRequest
}

func (f *fakeWindowProvider) DiscoverWindow(_ context.Context, req contract.DiscoverRequest) (contract.Window, error) {
	f.calls = append(f.calls, req)
	idx := len(f.calls) - 1
	if idx >= len(f.results) {
		return contract.Window{}, fmt.Errorf("unexpected discovery call %d", idx+1)
	}
	return f.results[idx].window, f.results[idx].err
}

// testWindow 构造一个合法窗口。
func testWindow(start, end time.Time) contract.Window {
	return contract.Window{Start: start, End: end, Source: contract.WindowPlatform}
}

// newDiscovery fixture：两个候选号，凭据齐全，任务指向同一学校。
func newDiscoveryFixture() (*fakeDiscoveryAccounts, *fakeWindowProvider) {
	accounts := &fakeDiscoveryAccounts{
		candidates: []appaccount.Record{
			{ID: "acc-bad", SchoolID: "school-1"},
			{ID: "acc-good", SchoolID: "school-1"},
		},
		credentials: map[string]appaccount.SigningCredential{
			"acc-bad":  {SchoolID: "school-1", Phone: "13800000009", Password: "synthetic-secret-bad", DeviceCode: "dev-bad"},
			"acc-good": {SchoolID: "school-1", Phone: "13800000008", Password: "synthetic-secret-good", DeviceCode: "dev-good"},
		},
	}
	provider := &fakeWindowProvider{}
	return accounts, provider
}

// TestScheduleDiscovererRotatesPastBadCandidate 钉住「换下一个号」的主路径。
//
// 旧实现只取 created_at 最新的一条：那条是坏号时整次发现失败，
// 全校回退默认窗口。这里断言第二个候选被真正用上。
func TestScheduleDiscovererRotatesPastBadCandidate(t *testing.T) {
	start := time.Date(2026, 11, 2, 9, 0, 0, 0, time.UTC)
	end := start.Add(2 * time.Hour)
	accounts, provider := newDiscoveryFixture()
	provider.results = []providerResult{
		{err: &contract.OutcomeError{Outcome: contract.OutcomeCredentialBad, Message: "login rejected"}},
		{window: testWindow(start, end)},
	}
	var logs bytes.Buffer
	d := scheduleDiscoverer{
		provider: provider,
		accounts: accounts,
		tasks:    fakeDiscoveryTasks{info: coreschedule.TaskInfo{ID: "task-1", SchoolID: "school-1", Enabled: true}},
		Logger:   slog.New(slog.NewTextHandler(&logs, nil)),
	}

	window, err := d.DiscoverWindow(context.Background(), "task-1")
	if err != nil {
		t.Fatalf("DiscoverWindow: %v", err)
	}
	if len(provider.calls) != 2 {
		t.Fatalf("probed %d candidates, want 2", len(provider.calls))
	}
	if got := provider.calls[1].Account.ID; got != "acc-good" {
		t.Fatalf("second probe used %q, want acc-good", got)
	}
	if !window.Start.Equal(start) || !window.End.Equal(end) {
		t.Fatalf("window = %s..%s, want %s..%s", window.Start, window.End, start, end)
	}
	if accounts.lastSchoolID != "school-1" || accounts.lastLimit != discoveryCandidates {
		t.Fatalf("candidate query = school %q limit %d, want school-1/%d",
			accounts.lastSchoolID, accounts.lastLimit, discoveryCandidates)
	}
	if got := strings.Join(accounts.credentialCalls, ","); got != "acc-bad,acc-good" {
		t.Fatalf("credential reads = %s, want acc-bad,acc-good", got)
	}

	logged := strings.ReplaceAll(logs.String(), "\"", "")
	for _, want := range []string{"account_id=acc-bad", "school_id=school-1", "outcome=credential_invalid"} {
		if !strings.Contains(logged, want) {
			t.Fatalf("candidate failure log misses %q: %s", want, logged)
		}
	}
	for _, leak := range []string{"13800000009", "synthetic-secret", "dev-bad", "@"} {
		if strings.Contains(logged, leak) {
			t.Fatalf("candidate failure log leaks %q: %s", leak, logged)
		}
	}
}

// TestScheduleDiscovererStopsOnPause 钉住急停不轮换。
//
// 急停不是「这个号坏了」：继续换号只会把停机响应拉长，且会让
// 上层把急停误当账号问题记一次告警。
func TestScheduleDiscovererStopsOnPause(t *testing.T) {
	accounts, provider := newDiscoveryFixture()
	provider.results = []providerResult{{err: coreerrs.NewPaused()}}
	// Logger 留空：装配漏注入时不得 panic。
	d := scheduleDiscoverer{
		provider: provider,
		accounts: accounts,
		tasks:    fakeDiscoveryTasks{info: coreschedule.TaskInfo{ID: "task-1", SchoolID: "school-1"}},
	}

	_, err := d.DiscoverWindow(context.Background(), "task-1")
	if err == nil {
		t.Fatal("pause returned no error")
	}
	if len(provider.calls) != 1 {
		t.Fatalf("probed %d candidates after pause, want 1", len(provider.calls))
	}
	var paused *coreerrs.PausedError
	if !errors.As(err, &paused) {
		t.Fatalf("pause error is %v, want PausedError", err)
	}
}

// TestScheduleDiscovererStopsOnContextCancel 钉住取消不轮换、不吞错误。
//
// 取消若被轮换吞掉，上层会把一次停机当成账号池耗尽。
func TestScheduleDiscovererStopsOnContextCancel(t *testing.T) {
	accounts, provider := newDiscoveryFixture()
	provider.results = []providerResult{{err: fmt.Errorf("login: %w", context.Canceled)}}
	d := scheduleDiscoverer{
		provider: provider,
		accounts: accounts,
		tasks:    fakeDiscoveryTasks{info: coreschedule.TaskInfo{ID: "task-1", SchoolID: "school-1"}},
	}

	_, err := d.DiscoverWindow(context.Background(), "task-1")
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("error = %v, want context.Canceled", err)
	}
	if len(provider.calls) != 1 {
		t.Fatalf("probed %d candidates after cancel, want 1", len(provider.calls))
	}
}

// TestScheduleDiscovererAllCandidatesFail 钉住「全坏」语义。
//
// TestScheduleDiscovererNormalOutcomeLogsInfo 钉住候选失败日志的分级：
// already_done 这类正常结论走 Info，只有需人处置的结论才 Warn。
// 否则全校已签到时每次发现会按候选数刷 Warn，淹没按级别告警的运维面。
func TestScheduleDiscovererNormalOutcomeLogsInfo(t *testing.T) {
	start := time.Date(2026, 11, 2, 9, 0, 0, 0, time.UTC)
	end := start.Add(2 * time.Hour)
	accounts, provider := newDiscoveryFixture()
	provider.results = []providerResult{
		{err: &contract.OutcomeError{Outcome: contract.OutcomeAlreadyDone, Message: "already signed"}},
		{window: testWindow(start, end)},
	}
	var logs bytes.Buffer
	d := scheduleDiscoverer{
		provider: provider,
		accounts: accounts,
		tasks:    fakeDiscoveryTasks{info: coreschedule.TaskInfo{ID: "task-1", SchoolID: "school-1"}},
		Logger:   slog.New(slog.NewTextHandler(&logs, nil)),
	}

	if _, err := d.DiscoverWindow(context.Background(), "task-1"); err != nil {
		t.Fatalf("DiscoverWindow: %v", err)
	}
	line := logs.String()
	if strings.Count(line, "candidate failed") != 1 {
		t.Fatalf("logged %q, want exactly one candidate failure", line)
	}
	if !strings.Contains(line, "level=INFO") {
		t.Fatalf("normal outcome logged above INFO: %q", line)
	}
	if strings.Contains(line, "level=WARN") {
		t.Fatalf("normal outcome logged as WARN: %q", line)
	}
}

// 全部候选失败时探测 3 次并返回最后一个错误：上层据此记一次告警并回退，
// 不能返回空窗口伪装成功。
func TestScheduleDiscovererAllCandidatesFail(t *testing.T) {
	accounts := &fakeDiscoveryAccounts{
		candidates: []appaccount.Record{
			{ID: "acc-1", SchoolID: "school-1"},
			{ID: "acc-2", SchoolID: "school-1"},
			{ID: "acc-3", SchoolID: "school-1"},
		},
		credentials: map[string]appaccount.SigningCredential{
			"acc-1": {SchoolID: "school-1", Phone: "13800000011"},
			"acc-2": {SchoolID: "school-1", Phone: "13800000012"},
			"acc-3": {SchoolID: "school-1", Phone: "13800000013"},
		},
	}
	provider := &fakeWindowProvider{results: []providerResult{
		{err: &contract.OutcomeError{Outcome: contract.OutcomeCredentialBad, Message: "login rejected"}},
		{err: &contract.OutcomeError{Outcome: contract.OutcomeDeviceUnbound, Message: "device unbound"}},
		{err: &contract.OutcomeError{Outcome: contract.OutcomeBlocked, Message: "blocked"}},
	}}
	var logs bytes.Buffer
	d := scheduleDiscoverer{
		provider: provider,
		accounts: accounts,
		tasks:    fakeDiscoveryTasks{info: coreschedule.TaskInfo{ID: "task-1", SchoolID: "school-1"}},
		Logger:   slog.New(slog.NewTextHandler(&logs, nil)),
	}

	window, err := d.DiscoverWindow(context.Background(), "task-1")
	if err == nil {
		t.Fatal("all candidates failed but discovery succeeded")
	}
	if !window.Start.IsZero() || !window.End.IsZero() {
		t.Fatalf("failed discovery returned window %s..%s", window.Start, window.End)
	}
	if len(provider.calls) != discoveryCandidates {
		t.Fatalf("probed %d candidates, want %d", len(provider.calls), discoveryCandidates)
	}
	var outcomeErr *contract.OutcomeError
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomeBlocked {
		t.Fatalf("error = %v, want the last candidate's outcome %q", err, contract.OutcomeBlocked)
	}
	if got := strings.Count(logs.String(), "candidate failed"); got != discoveryCandidates {
		t.Fatalf("logged %d candidate failures, want %d", got, discoveryCandidates)
	}
}

// TestScheduleDiscovererEmptyCandidatesIsNotFound 钉住空候选语义。
//
// 空候选不是成功：沿用 not_found，计划层据此回退缓存或默认窗口。
func TestScheduleDiscovererEmptyCandidatesIsNotFound(t *testing.T) {
	accounts := &fakeDiscoveryAccounts{}
	provider := &fakeWindowProvider{}
	d := scheduleDiscoverer{
		provider: provider,
		accounts: accounts,
		tasks:    fakeDiscoveryTasks{info: coreschedule.TaskInfo{ID: "task-1", SchoolID: "school-1"}},
	}

	_, err := d.DiscoverWindow(context.Background(), "task-1")
	if err == nil {
		t.Fatal("empty candidate list returned a window")
	}
	if code := coreerrs.From(err).Code; code != coreerrs.CodeNotFound {
		t.Fatalf("code = %q, want not_found", code)
	}
	if len(provider.calls) != 0 {
		t.Fatalf("empty candidate list probed the provider %d times", len(provider.calls))
	}
}

// TestScheduleDiscovererRotatesPastInvalidWindow 钉住「合法窗口才算成功」。
//
// provider 无错返回零窗口或反向窗口时，把它当全校结论会让整校
// 排进坏窗口；这里换下一个候选，并把该次失败记成 upstream 分类。
func TestScheduleDiscovererRotatesPastInvalidWindow(t *testing.T) {
	start := time.Date(2026, 11, 2, 9, 0, 0, 0, time.UTC)
	end := start.Add(2 * time.Hour)
	accounts, provider := newDiscoveryFixture()
	provider.results = []providerResult{
		{window: contract.Window{Start: end, End: start, Source: contract.WindowPlatform}},
		{window: testWindow(start, end)},
	}
	var logs bytes.Buffer
	d := scheduleDiscoverer{
		provider: provider,
		accounts: accounts,
		tasks:    fakeDiscoveryTasks{info: coreschedule.TaskInfo{ID: "task-1", SchoolID: "school-1"}},
		Logger:   slog.New(slog.NewTextHandler(&logs, nil)),
	}
	// 另起一次发现覆盖「Logger 缺省」：nil 时回退进程默认 logger，不得 panic。
	memberOnly := scheduleDiscoverer{
		provider: &fakeWindowProvider{results: provider.results},
		accounts: accounts,
		tasks:    fakeDiscoveryTasks{info: coreschedule.TaskInfo{ID: "task-1", SchoolID: "school-1"}},
	}
	if _, err := memberOnly.DiscoverWindow(context.Background(), "task-1"); err != nil {
		t.Fatalf("nil logger discovery: %v", err)
	}

	window, err := d.DiscoverWindow(context.Background(), "task-1")
	if err != nil {
		t.Fatalf("DiscoverWindow: %v", err)
	}
	if len(provider.calls) != 2 {
		t.Fatalf("probed %d candidates, want 2", len(provider.calls))
	}
	if !window.Start.Equal(start) || !window.End.Equal(end) {
		t.Fatalf("window = %s..%s, want the second candidate's window", window.Start, window.End)
	}
	if logged := logs.String(); !strings.Contains(logged, "upstream_unavailable") {
		t.Fatalf("invalid window failure is not classified: %s", logged)
	}
}
