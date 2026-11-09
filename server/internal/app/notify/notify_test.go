package notify_test

import (
	"context"
	"errors"
	"testing"

	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// fakeRepo 在内存里保存覆盖集，语义与 pg 实现一致：Replace 整体替换。
type fakeRepo struct {
	overrides map[appnotify.Pair]bool
	replaced  [][]appnotify.Pref
	failWith  error
}

func (f *fakeRepo) Overrides(context.Context, string) (map[appnotify.Pair]bool, error) {
	if f.failWith != nil {
		return nil, f.failWith
	}
	out := make(map[appnotify.Pair]bool, len(f.overrides))
	for pair, enabled := range f.overrides {
		out[pair] = enabled
	}
	return out, nil
}

func (f *fakeRepo) Replace(_ context.Context, _ string, prefs []appnotify.Pref) error {
	if f.failWith != nil {
		return f.failWith
	}
	f.replaced = append(f.replaced, append([]appnotify.Pref(nil), prefs...))
	f.overrides = make(map[appnotify.Pair]bool, len(prefs))
	for _, pref := range prefs {
		f.overrides[appnotify.Pair{Category: pref.Category, Channel: pref.Channel}] = pref.Enabled
	}
	return nil
}

func newService(t *testing.T, repo appnotify.Repo) *appnotify.Service {
	t.Helper()
	svc, err := appnotify.NewService(repo)
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// 缺行必须按默认解释，且覆盖全部十二对、顺序稳定。
func TestGetAppliesDefaults(t *testing.T) {
	svc := newService(t, &fakeRepo{})
	page, err := svc.Get(context.Background(), "user-1")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if len(page.Items) != 12 {
		t.Fatalf("items = %d, want 12 (4 categories x 3 channels)", len(page.Items))
	}
	want := map[appnotify.Pair]bool{
		{Category: "review_pending", Channel: "in_app"}:      true,
		{Category: "review_pending", Channel: "email"}:       true,
		{Category: "review_pending", Channel: "webhook"}:     true,
		{Category: "credential_invalid", Channel: "in_app"}:  true,
		{Category: "credential_invalid", Channel: "email"}:   false,
		{Category: "credential_invalid", Channel: "webhook"}: true,
		{Category: "sign_failed_daily", Channel: "in_app"}:   false,
		{Category: "sign_failed_daily", Channel: "email"}:    true,
		{Category: "sign_failed_daily", Channel: "webhook"}:  false,
		{Category: "capacity_rejected", Channel: "in_app"}:   true,
		{Category: "capacity_rejected", Channel: "email"}:    false,
		{Category: "capacity_rejected", Channel: "webhook"}:  false,
	}
	for _, item := range page.Items {
		pair := appnotify.Pair{Category: item.Category, Channel: item.Channel}
		enabled, ok := want[pair]
		if !ok {
			t.Fatalf("unexpected pair %v", pair)
		}
		if item.Enabled != enabled {
			t.Fatalf("%v enabled = %t, want %t", pair, item.Enabled, enabled)
		}
	}
	// 顺序固定：类别按词表，渠道 in_app 在前。
	if page.Items[0].Category != appnotify.CategoryCredentialInvalid ||
		page.Items[1].Channel != appnotify.ChannelEmail {
		t.Fatalf("unstable order: %+v", page.Items[:2])
	}
}

// 显式覆盖优先于默认；关闭与开启都按覆盖读取。
func TestGetUsesStoredOverrides(t *testing.T) {
	repo := &fakeRepo{overrides: map[appnotify.Pair]bool{
		{Category: appnotify.CategoryReviewPending, Channel: appnotify.ChannelEmail}:   false,
		{Category: appnotify.CategorySignFailedDaily, Channel: appnotify.ChannelInApp}: true,
	}}
	page, err := newService(t, repo).Get(context.Background(), "user-1")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	for _, item := range page.Items {
		switch {
		case item.Category == appnotify.CategoryReviewPending && item.Channel == appnotify.ChannelEmail:
			if item.Enabled {
				t.Fatal("stored override false was not honoured")
			}
		case item.Category == appnotify.CategorySignFailedDaily && item.Channel == appnotify.ChannelInApp:
			if !item.Enabled {
				t.Fatal("stored override true was not honoured")
			}
		}
	}
}

// PUT 空列表必须清空覆盖并回到默认。
func TestSetEmptyResetsToDefaults(t *testing.T) {
	repo := &fakeRepo{overrides: map[appnotify.Pair]bool{
		{Category: appnotify.CategoryReviewPending, Channel: appnotify.ChannelEmail}: false,
	}}
	page, err := newService(t, repo).Set(context.Background(), "user-1", []appnotify.Pref{})
	if err != nil {
		t.Fatalf("Set: %v", err)
	}
	if len(repo.replaced) != 1 || len(repo.replaced[0]) != 0 {
		t.Fatalf("replace calls = %+v, want one empty replacement", repo.replaced)
	}
	if len(repo.overrides) != 0 {
		t.Fatalf("overrides = %v, want empty", repo.overrides)
	}
	for _, item := range page.Items {
		pair := appnotify.Pair{Category: item.Category, Channel: item.Channel}
		if item.Enabled != appnotify.Default(pair) {
			t.Fatalf("%v = %t, want default %t", pair, item.Enabled, appnotify.Default(pair))
		}
	}
}

// 替换是完整覆盖集：未列出的对回默认，列出的对按请求值。
func TestSetReplacesWholeSet(t *testing.T) {
	repo := &fakeRepo{overrides: map[appnotify.Pair]bool{
		{Category: appnotify.CategoryCapacityRejected, Channel: appnotify.ChannelEmail}: true,
	}}
	page, err := newService(t, repo).Set(context.Background(), "user-1", []appnotify.Pref{
		{Category: appnotify.CategoryCapacityRejected, Channel: appnotify.ChannelEmail, Enabled: false},
	})
	if err != nil {
		t.Fatalf("Set: %v", err)
	}
	for _, item := range page.Items {
		if item.Category == appnotify.CategoryCapacityRejected && item.Channel == appnotify.ChannelEmail {
			if item.Enabled {
				t.Fatal("listed pair must keep the request value")
			}
			continue
		}
		pair := appnotify.Pair{Category: item.Category, Channel: item.Channel}
		if item.Enabled != appnotify.Default(pair) {
			t.Fatalf("%v = %t, want default after replacement", pair, item.Enabled)
		}
	}
}

// 负向：未知类别、未知渠道、重复对、缺失 items 都是校验失败，且不写存储。
func TestSetRejectsInvalidInput(t *testing.T) {
	cases := map[string][]appnotify.Pref{
		"unknown category": {{Category: "weekly_digest", Channel: appnotify.ChannelInApp}},
		"unknown channel":  {{Category: appnotify.CategoryReviewPending, Channel: "sms"}},
		"duplicate pair": {
			{Category: appnotify.CategoryReviewPending, Channel: appnotify.ChannelEmail, Enabled: true},
			{Category: appnotify.CategoryReviewPending, Channel: appnotify.ChannelEmail, Enabled: false},
		},
		"missing items": nil,
	}
	for name, items := range cases {
		t.Run(name, func(t *testing.T) {
			repo := &fakeRepo{}
			_, err := newService(t, repo).Set(context.Background(), "user-1", items)
			if err == nil {
				t.Fatal("Set must fail")
			}
			var appErr *errs.Error
			if !errors.As(err, &appErr) || appErr.Code != errs.CodeValidation {
				t.Fatalf("error = %v, want validation_failed", err)
			}
			if len(repo.replaced) != 0 {
				t.Fatalf("storage replaced %d times, want 0", len(repo.replaced))
			}
		})
	}
}

// 仓储错误原样上抛，不吞成空页。
func TestGetPropagatesRepoError(t *testing.T) {
	boom := errors.New("boom")
	_, err := newService(t, &fakeRepo{failWith: boom}).Get(context.Background(), "user-1")
	if !errors.Is(err, boom) {
		t.Fatalf("err = %v, want boom", err)
	}
}
