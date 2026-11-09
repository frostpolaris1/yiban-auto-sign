package settings_test

// 覆盖链解析器（sett2）的守卫单测。
//
// 判据：DB 行存在且合法→行值；行缺失→env 值；
// 坏行（手工 SQL 写坏）→回退 env 且每键只 Warn 一次（不记值原文）；
// 存储故障原样上抛（故障不是配置状态）；域校验唯一源是注入的名册域。

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"strconv"
	"strings"
	"testing"

	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
)

// fakeOverrideStore 是内存覆盖行仓储：rows 存原始行值，
// readErr/writeErr 注入存储故障。行值由测试直接写成合法或非法字面。
type fakeOverrideStore struct {
	rows    map[string]string
	readErr error
	// badInt/badBool 让读路径返回校验错（模拟 pg 对坏行的判定）。
	badInt  bool
	badBool bool
}

func (f *fakeOverrideStore) IntOverride(context.Context, string) (int, bool, error) {
	if f.readErr != nil {
		return 0, false, f.readErr
	}
	raw, ok := f.rows["int"]
	if !ok {
		return 0, false, nil
	}
	if f.badInt {
		return 0, true, errs.New(errs.CodeValidation, "settings value is not an integer")
	}
	value := 0
	for _, ch := range raw {
		if ch < '0' || ch > '9' {
			return 0, true, errs.New(errs.CodeValidation, "settings value is not an integer")
		}
		value = value*10 + int(ch-'0')
	}
	return value, true, nil
}

func (f *fakeOverrideStore) BoolOverride(context.Context, string) (bool, bool, error) {
	if f.readErr != nil {
		return false, false, f.readErr
	}
	raw, ok := f.rows["bool"]
	if !ok {
		return false, false, nil
	}
	if f.badBool {
		return false, true, errs.New(errs.CodeValidation, "settings value is not a boolean literal")
	}
	switch raw {
	case "true":
		return true, true, nil
	case "false":
		return false, true, nil
	default:
		return false, true, errs.New(errs.CodeValidation, "settings value is not a boolean literal")
	}
}

func (f *fakeOverrideStore) SetInt(context.Context, string, int, string) error { return nil }

func (f *fakeOverrideStore) SetBool(context.Context, string, bool, string) error { return nil }

func (f *fakeOverrideStore) ClearSetting(context.Context, string) error { return nil }

// newTestResolver 构造带日志捕获的解析器；两枚 edge 键都带 [0,300] 域。
func newTestResolver(t *testing.T, store appsettings.OverrideReader, log *slog.Logger) *appsettings.Resolver {
	t.Helper()
	resolver, err := appsettings.NewResolver(store, map[string]appsettings.SettingDomain{
		appsettings.KeyScheduleEdgeFrontSec: {Min: 0, Max: 300},
		appsettings.KeyScheduleEdgeBackSec:  {Min: 0, Max: 300},
	}, log)
	if err != nil {
		t.Fatalf("NewResolver: %v", err)
	}
	return resolver
}

// warnLines 统计日志里坏值告警的行数。
func warnLines(buf *bytes.Buffer) int {
	count := 0
	for _, line := range strings.Split(buf.String(), "\n") {
		if strings.Contains(line, "settings.override_invalid") {
			count++
		}
	}
	return count
}

// 无 DB 行时读取值与 env 完全一致，零告警。
func TestResolveFallsBackToEnvWithoutRow(t *testing.T) {
	var buf bytes.Buffer
	resolver := newTestResolver(t, &fakeOverrideStore{rows: map[string]string{}}, slog.New(slog.NewTextHandler(&buf, nil)))

	value, err := resolver.ResolveInt(context.Background(), appsettings.KeyScheduleEdgeFrontSec, 60)
	if err != nil || value != 60 {
		t.Fatalf("ResolveInt = (%d, %v), want env 60", value, err)
	}
	allowed, err := resolver.ResolveBool(context.Background(), appsettings.KeyScheduleAllowTimePref, false)
	if err != nil || allowed {
		t.Fatalf("ResolveBool = (%v, %v), want env false", allowed, err)
	}
	if warnLines(&buf) != 0 {
		t.Fatalf("缺行不得告警: %q", buf.String())
	}
}

// DB 行存在且合法时覆盖 env；布尔行读出字面值。
func TestResolvePrefersValidDBRow(t *testing.T) {
	resolver := newTestResolver(t, &fakeOverrideStore{rows: map[string]string{
		"int": "120", "bool": "true",
	}}, nil)

	value, err := resolver.ResolveInt(context.Background(), appsettings.KeyScheduleEdgeFrontSec, 60)
	if err != nil || value != 120 {
		t.Fatalf("ResolveInt = (%d, %v), want db 120", value, err)
	}
	allowed, err := resolver.ResolveBool(context.Background(), appsettings.KeyScheduleAllowTimePref, false)
	if err != nil || !allowed {
		t.Fatalf("ResolveBool = (%v, %v), want db true", allowed, err)
	}
}

// 坏行回退 env 并每键只 Warn 一次；日志不落值原文。
func TestResolveInvalidRowFallsBackWithOneWarn(t *testing.T) {
	var buf bytes.Buffer
	resolver := newTestResolver(t, &fakeOverrideStore{
		rows: map[string]string{"int": "abc", "bool": "TRUE"}, badInt: true, badBool: true,
	}, slog.New(slog.NewTextHandler(&buf, nil)))
	ctx := context.Background()

	for i := 0; i < 3; i++ {
		value, err := resolver.ResolveInt(ctx, appsettings.KeyScheduleEdgeFrontSec, 60)
		if err != nil || value != 60 {
			t.Fatalf("bad row ResolveInt = (%d, %v), want env 60", value, err)
		}
		allowed, err := resolver.ResolveBool(ctx, appsettings.KeyScheduleAllowTimePref, false)
		if err != nil || allowed {
			t.Fatalf("bad row ResolveBool = (%v, %v), want env false", allowed, err)
		}
	}
	if got := warnLines(&buf); got != 2 {
		t.Fatalf("坏值告警行数 = %d, want 2（每键一次）", got)
	}
	if strings.Contains(buf.String(), "abc") || strings.Contains(buf.String(), "TRUE") {
		t.Fatalf("告警不得落值原文: %q", buf.String())
	}
}

// 域外 DB 行同样回退 env：写面域与读面域是同一份。
func TestResolveOutOfDomainRowFallsBack(t *testing.T) {
	var buf bytes.Buffer
	resolver := newTestResolver(t, &fakeOverrideStore{rows: map[string]string{"int": "301"}}, slog.New(slog.NewTextHandler(&buf, nil)))
	value, err := resolver.ResolveInt(context.Background(), appsettings.KeyScheduleEdgeFrontSec, 60)
	if err != nil || value != 60 {
		t.Fatalf("out-of-domain row = (%d, %v), want env 60", value, err)
	}
	if warnLines(&buf) != 1 {
		t.Fatalf("域外行告警 = %d, want 1", warnLines(&buf))
	}
}

// 存储故障原样上抛：故障不是配置状态，不得静默当 env。
func TestResolvePropagatesStoreFailure(t *testing.T) {
	storeErr := errors.New("db down")
	resolver := newTestResolver(t, &fakeOverrideStore{readErr: storeErr}, nil)
	if _, err := resolver.ResolveInt(context.Background(), appsettings.KeyScheduleEdgeFrontSec, 60); !errors.Is(err, storeErr) {
		t.Fatalf("store failure err = %v, want the raw store error", err)
	}
	if _, err := resolver.ResolveBool(context.Background(), appsettings.KeyScheduleAllowTimePref, false); !errors.Is(err, storeErr) {
		t.Fatalf("store failure err = %v, want the raw store error", err)
	}
}

// 域校验：越域与未知键都拒绝；边界值放行。
func TestCheckIntDomain(t *testing.T) {
	resolver := newTestResolver(t, &fakeOverrideStore{}, nil)
	for _, value := range []int{-1, 301, 1000} {
		if err := resolver.CheckInt(appsettings.KeyScheduleEdgeFrontSec, value); err == nil ||
			errs.From(err).Code != errs.CodeValidation {
			t.Fatalf("CheckInt(%d) err = %v, want validation", value, err)
		}
	}
	for _, value := range []int{0, 60, 300} {
		if err := resolver.CheckInt(appsettings.KeyScheduleEdgeFrontSec, value); err != nil {
			t.Fatalf("CheckInt(%d) = %v, want nil", value, err)
		}
	}
	if err := resolver.CheckInt("not.a.key", 1); err == nil {
		t.Fatal("未知键不得放行")
	}
}

// 名册读数：行存在给解析值，坏行按未覆盖；Known 只认写回键；
// 名册键（YIBAN_*）经别名落到同一覆盖行。
func TestLookupForRoster(t *testing.T) {
	var buf bytes.Buffer
	resolver := newTestResolver(t, &fakeOverrideStore{rows: map[string]string{
		"int": "90", "bool": "false",
	}}, slog.New(slog.NewTextHandler(&buf, nil)))

	found, value, err := resolver.Lookup(context.Background(), appsettings.KeyScheduleEdgeFrontSec)
	if err != nil || !found || value != 90 {
		t.Fatalf("Lookup int = (%v, %v, %v)", found, value, err)
	}
	found, value, err = resolver.Lookup(context.Background(), appsettings.KeyScheduleAllowTimePref)
	if err != nil || !found || value != false {
		t.Fatalf("Lookup bool = (%v, %v, %v)", found, value, err)
	}
	// 名册键别名：设置页以 YIBAN_* 查询，读到同一覆盖行。
	found, value, err = resolver.Lookup(context.Background(), "YIBAN_WINDOW_EDGE_FRONT_SEC")
	if err != nil || !found || value != 90 {
		t.Fatalf("Lookup alias = (%v, %v, %v), want db 90", found, value, err)
	}
	if resolver.Known("YIBAN_WORKERS") || resolver.Known("registration.enabled") {
		t.Fatal("非写回键不得进入覆盖查询")
	}
	if !resolver.Known(appsettings.KeyScheduleEdgeBackSec) || !resolver.Known("YIBAN_WINDOW_EDGE_BACK_SEC") {
		t.Fatal("写回键（行键与名册键两种形态）必须 Known")
	}
}

// ===== 批 3b：默认签到窗口对解析（schedule.default_sign_start_min/end_min）=====

// keyedOverrideStore 是按键区分行的覆盖行替身：窗口对解析要对
// start/end 两键读出不同状态（缺行/行值/坏行/存储故障）。
// 行值写成非整数串即模拟 pg 侧的坏行校验错。
type keyedOverrideStore struct {
	intRows  map[string]string
	boolRows map[string]string
	readErr  error
}

func (f *keyedOverrideStore) IntOverride(_ context.Context, key string) (int, bool, error) {
	if f.readErr != nil {
		return 0, false, f.readErr
	}
	raw, ok := f.intRows[key]
	if !ok {
		return 0, false, nil
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return 0, true, errs.New(errs.CodeValidation, "settings value is not an integer")
	}
	return value, true, nil
}

func (f *keyedOverrideStore) BoolOverride(_ context.Context, key string) (bool, bool, error) {
	if f.readErr != nil {
		return false, false, f.readErr
	}
	raw, ok := f.boolRows[key]
	if !ok {
		return false, false, nil
	}
	switch raw {
	case "true":
		return true, true, nil
	case "false":
		return false, true, nil
	default:
		return false, true, errs.New(errs.CodeValidation, "settings value is not a boolean literal")
	}
}

func (f *keyedOverrideStore) SetInt(_ context.Context, key string, value int, _ string) error {
	if f.intRows == nil {
		f.intRows = map[string]string{}
	}
	f.intRows[key] = strconv.Itoa(value)
	return nil
}

func (f *keyedOverrideStore) SetBool(_ context.Context, key string, value bool, _ string) error {
	if f.boolRows == nil {
		f.boolRows = map[string]string{}
	}
	f.boolRows[key] = strconv.FormatBool(value)
	return nil
}

func (f *keyedOverrideStore) ClearSetting(_ context.Context, key string) error {
	delete(f.intRows, key)
	delete(f.boolRows, key)
	return nil
}

// newKeyedResolver 构造带日志捕获的解析器（keyed store；域仍只注入 edge 两键——
// 窗口对键是 DB-only，域不走 registry 注入）。
func newKeyedResolver(t *testing.T, store *keyedOverrideStore, log *slog.Logger) *appsettings.Resolver {
	t.Helper()
	return newTestResolver(t, store, log)
}

// 双缺行是常态：静默用常量对，零告警。
func TestResolveSignWindowFallsBackWithoutRows(t *testing.T) {
	var buf bytes.Buffer
	resolver := newKeyedResolver(t, &keyedOverrideStore{intRows: map[string]string{}},
		slog.New(slog.NewTextHandler(&buf, nil)))
	start, end, err := resolver.ResolveSignWindow(context.Background(),
		coreschedule.DefaultSignWindowStartMinute, coreschedule.DefaultSignWindowEndMinute)
	if err != nil || start != coreschedule.DefaultSignWindowStartMinute || end != coreschedule.DefaultSignWindowEndMinute {
		t.Fatalf("ResolveSignWindow = (%d, %d, %v), want builtin constants", start, end, err)
	}
	if warnLines(&buf) != 0 {
		t.Fatalf("双缺行不得告警: %q", buf.String())
	}
}

// 两侧行都在且合法：整对用行值。
func TestResolveSignWindowUsesStoredPair(t *testing.T) {
	resolver := newKeyedResolver(t, &keyedOverrideStore{intRows: map[string]string{
		appsettings.KeyScheduleDefaultSignStartMin: "480",
		appsettings.KeyScheduleDefaultSignEndMin:   "540",
	}}, nil)
	start, end, err := resolver.ResolveSignWindow(context.Background(),
		coreschedule.DefaultSignWindowStartMinute, coreschedule.DefaultSignWindowEndMinute)
	if err != nil || start != 480 || end != 540 {
		t.Fatalf("ResolveSignWindow = (%d, %d, %v), want stored pair (480, 540)", start, end, err)
	}
}

// 半对：一侧有行一侧缺行，整对回退常量并告警一次（防跨语义拼接）。
func TestResolveSignWindowHalfPairFallsBackWithWarn(t *testing.T) {
	var buf bytes.Buffer
	store := &keyedOverrideStore{intRows: map[string]string{
		appsettings.KeyScheduleDefaultSignStartMin: "480",
	}}
	resolver := newKeyedResolver(t, store, slog.New(slog.NewTextHandler(&buf, nil)))
	start, end, err := resolver.ResolveSignWindow(context.Background(),
		coreschedule.DefaultSignWindowStartMinute, coreschedule.DefaultSignWindowEndMinute)
	if err != nil || start != coreschedule.DefaultSignWindowStartMinute || end != coreschedule.DefaultSignWindowEndMinute {
		t.Fatalf("half pair start = (%d, %d, %v), want builtin pair", start, end, err)
	}
	if warnLines(&buf) != 1 {
		t.Fatalf("半对必须告警恰一次: %q", buf.String())
	}
	// 反向半对：只有 end 行，同样整对回退。
	store2 := &keyedOverrideStore{intRows: map[string]string{
		appsettings.KeyScheduleDefaultSignEndMin: "540",
	}}
	var buf2 bytes.Buffer
	resolver2 := newKeyedResolver(t, store2, slog.New(slog.NewTextHandler(&buf2, nil)))
	start, end, err = resolver2.ResolveSignWindow(context.Background(),
		coreschedule.DefaultSignWindowStartMinute, coreschedule.DefaultSignWindowEndMinute)
	if err != nil || start != coreschedule.DefaultSignWindowStartMinute || end != coreschedule.DefaultSignWindowEndMinute {
		t.Fatalf("half pair end = (%d, %d, %v), want builtin pair", start, end, err)
	}
	if warnLines(&buf2) != 1 {
		t.Fatalf("反向半对必须告警恰一次: %q", buf2.String())
	}
}

// 非法对：越域（1440、-1）与 start>=end 都整对回退常量并告警。
func TestResolveSignWindowInvalidPairFallsBack(t *testing.T) {
	for name, rows := range map[string]map[string]string{
		"start_above_domain": {"schedule.default_sign_start_min": "1440", "schedule.default_sign_end_min": "1500"},
		"end_negative":       {"schedule.default_sign_start_min": "-1", "schedule.default_sign_end_min": "500"},
		"start_equals_end":   {"schedule.default_sign_start_min": "500", "schedule.default_sign_end_min": "500"},
		"start_after_end":    {"schedule.default_sign_start_min": "500", "schedule.default_sign_end_min": "400"},
	} {
		var buf bytes.Buffer
		resolver := newKeyedResolver(t, &keyedOverrideStore{intRows: rows},
			slog.New(slog.NewTextHandler(&buf, nil)))
		start, end, err := resolver.ResolveSignWindow(context.Background(),
			coreschedule.DefaultSignWindowStartMinute, coreschedule.DefaultSignWindowEndMinute)
		if err != nil || start != coreschedule.DefaultSignWindowStartMinute || end != coreschedule.DefaultSignWindowEndMinute {
			t.Fatalf("%s: ResolveSignWindow = (%d, %d, %v), want builtin pair", name, start, end, err)
		}
		if warnLines(&buf) != 1 {
			t.Fatalf("%s: 非法对必须告警恰一次: %q", name, buf.String())
		}
	}
}

// 坏行（手工 SQL 写坏）：整对回退常量并告警，不把坏值原文带出。
func TestResolveSignWindowBadRowFallsBack(t *testing.T) {
	var buf bytes.Buffer
	resolver := newKeyedResolver(t, &keyedOverrideStore{intRows: map[string]string{
		appsettings.KeyScheduleDefaultSignStartMin: "abc",
		appsettings.KeyScheduleDefaultSignEndMin:   "540",
	}}, slog.New(slog.NewTextHandler(&buf, nil)))
	start, end, err := resolver.ResolveSignWindow(context.Background(),
		coreschedule.DefaultSignWindowStartMinute, coreschedule.DefaultSignWindowEndMinute)
	if err != nil || start != coreschedule.DefaultSignWindowStartMinute || end != coreschedule.DefaultSignWindowEndMinute {
		t.Fatalf("bad row = (%d, %d, %v), want builtin pair", start, end, err)
	}
	if warnLines(&buf) != 1 {
		t.Fatalf("坏行必须告警恰一次: %q", buf.String())
	}
}

// 存储故障原样上抛：故障不是配置状态，不得当缺行回退。
func TestResolveSignWindowPropagatesStoreFailure(t *testing.T) {
	resolver := newKeyedResolver(t, &keyedOverrideStore{readErr: errors.New("db down")}, nil)
	if _, _, err := resolver.ResolveSignWindow(context.Background(),
		coreschedule.DefaultSignWindowStartMinute, coreschedule.DefaultSignWindowEndMinute); err == nil {
		t.Fatal("store failure must propagate")
	}
}

// 对级告警每进程只出一次：连续两次解析只记一条 Warn。
func TestResolveSignWindowWarnsOnce(t *testing.T) {
	var buf bytes.Buffer
	resolver := newKeyedResolver(t, &keyedOverrideStore{intRows: map[string]string{
		appsettings.KeyScheduleDefaultSignStartMin: "480",
	}}, slog.New(slog.NewTextHandler(&buf, nil)))
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		if _, _, err := resolver.ResolveSignWindow(ctx,
			coreschedule.DefaultSignWindowStartMinute, coreschedule.DefaultSignWindowEndMinute); err != nil {
			t.Fatalf("call %d: %v", i, err)
		}
	}
	if warnLines(&buf) != 1 {
		t.Fatalf("warn count = %d, want 1: %q", warnLines(&buf), buf.String())
	}
}

// 构造失败面：缺仓储、缺域、缺单键域都不得构造成功。
func TestNewResolverRejectsIncompleteWiring(t *testing.T) {
	if _, err := appsettings.NewResolver(nil, map[string]appsettings.SettingDomain{
		appsettings.KeyScheduleEdgeFrontSec: {Min: 0, Max: 300},
		appsettings.KeyScheduleEdgeBackSec:  {Min: 0, Max: 300},
	}, nil); err == nil {
		t.Fatal("nil store must fail")
	}
	if _, err := appsettings.NewResolver(&fakeOverrideStore{}, nil, nil); err == nil {
		t.Fatal("empty domains must fail")
	}
	if _, err := appsettings.NewResolver(&fakeOverrideStore{}, map[string]appsettings.SettingDomain{
		appsettings.KeyScheduleEdgeFrontSec: {Min: 0, Max: 300},
	}, nil); err == nil {
		t.Fatal("missing edge domain must fail")
	}
}
