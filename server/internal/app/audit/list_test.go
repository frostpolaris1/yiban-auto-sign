package audit_test

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// fakeLister 记录查询并返回固定页。
type fakeLister struct {
	rows   []coreaudit.LogRow
	next   string
	query  coreaudit.LogQuery
	err    error
	called int
}

func (f *fakeLister) ListLogs(_ context.Context, query coreaudit.LogQuery) ([]coreaudit.LogRow, string, error) {
	f.called++
	f.query = query
	if f.err != nil {
		return nil, "", f.err
	}
	return f.rows, f.next, nil
}

func newReader(t *testing.T, lister *fakeLister) *appaudit.Reader {
	t.Helper()
	reader, err := appaudit.NewReader(lister)
	if err != nil {
		t.Fatalf("NewReader: %v", err)
	}
	return reader
}

// actor 过滤值按写入口径掩码：完整邮箱变成存储标签。
func TestListMasksActorFilter(t *testing.T) {
	cases := map[string]string{
		"alice@example.com":  "a***@example.com",
		"ALICE@example.com":  "a***@example.com",
		" a***@example.com ": "a***@example.com",
		"unknown":            "unknown",
		"":                   "",
	}
	for input, want := range cases {
		lister := &fakeLister{}
		_, err := newReader(t, lister).List(context.Background(), appaudit.ListFilter{Actor: input})
		if err != nil {
			t.Fatalf("List(%q): %v", input, err)
		}
		if lister.query.Actor != want {
			t.Fatalf("actor filter %q -> %q, want %q", input, lister.query.Actor, want)
		}
	}
}

// detail 出口过掩码：手机号与邮箱不得原样回给客户端。
func TestListMasksDetail(t *testing.T) {
	lister := &fakeLister{rows: []coreaudit.LogRow{{
		ID: 1, TS: time.Date(2026, 11, 4, 8, 0, 0, 0, time.UTC), Action: "auth.login",
		ActorLabel: "a***@example.com",
		Detail:     `{"reason":"联系电话 13900139000 邮箱 alice@example.com"}`,
	}}}
	page, err := newReader(t, lister).List(context.Background(), appaudit.ListFilter{})
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	detail := page.Items[0].Detail
	if strings.Contains(detail, "13900139000") || strings.Contains(detail, "alice@example.com") {
		t.Fatalf("detail leaked PII: %q", detail)
	}
	if !strings.Contains(detail, "139****9000") || !strings.Contains(detail, "a***@example.com") {
		t.Fatalf("detail was not masked as expected: %q", detail)
	}
}

// 缺省 limit 是 50；显式越界 400。
func TestListLimit(t *testing.T) {
	lister := &fakeLister{}
	if _, err := newReader(t, lister).List(context.Background(), appaudit.ListFilter{}); err != nil {
		t.Fatalf("List: %v", err)
	}
	if lister.query.Limit != 50 {
		t.Fatalf("limit = %d, want 50", lister.query.Limit)
	}
	for _, limit := range []int{-1, 201} {
		_, err := newReader(t, &fakeLister{}).List(context.Background(), appaudit.ListFilter{Limit: limit})
		var appErr *errs.Error
		if !errors.As(err, &appErr) || appErr.Code != errs.CodeValidation {
			t.Fatalf("limit %d err = %v, want validation_failed", limit, err)
		}
	}
}

// 负向：超长 action/actor 与倒置时间界都是 400，且不读仓储。
func TestListRejectsInvalidFilters(t *testing.T) {
	from := time.Date(2026, 11, 4, 10, 0, 0, 0, time.UTC)
	to := from.Add(-time.Hour)
	cases := map[string]appaudit.ListFilter{
		"long action": {Action: strings.Repeat("a", 129)},
		"long actor":  {Actor: strings.Repeat("a", 255)},
		"inverted":    {From: &from, To: &to},
	}
	for name, filter := range cases {
		t.Run(name, func(t *testing.T) {
			lister := &fakeLister{}
			_, err := newReader(t, lister).List(context.Background(), filter)
			var appErr *errs.Error
			if !errors.As(err, &appErr) || appErr.Code != errs.CodeValidation {
				t.Fatalf("err = %v, want validation_failed", err)
			}
			if lister.called != 0 {
				t.Fatal("invalid filter reached the repository")
			}
		})
	}
}

// 未知 action 不报错：返回空页。
func TestListUnknownActionIsEmpty(t *testing.T) {
	lister := &fakeLister{}
	page, err := newReader(t, lister).List(context.Background(), appaudit.ListFilter{Action: "auth.nope"})
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(page.Items) != 0 || page.NextCursor != "" {
		t.Fatalf("page = %+v, want empty", page)
	}
	if lister.query.Action != "auth.nope" {
		t.Fatalf("action = %q", lister.query.Action)
	}
}

// 时间界与游标原样透传，游标由仓储解释。
func TestListPassesBoundsAndCursor(t *testing.T) {
	from := time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)
	to := from.Add(24 * time.Hour)
	lister := &fakeLister{next: "next-cursor"}
	page, err := newReader(t, lister).List(context.Background(), appaudit.ListFilter{
		From: &from, To: &to, Cursor: "opaque", Limit: 10,
	})
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if lister.query.From == nil || !lister.query.From.Equal(from) ||
		lister.query.To == nil || !lister.query.To.Equal(to) {
		t.Fatalf("bounds = %v/%v", lister.query.From, lister.query.To)
	}
	if lister.query.Cursor != "opaque" || page.NextCursor != "next-cursor" {
		t.Fatalf("cursor passthrough failed: query=%q next=%q", lister.query.Cursor, page.NextCursor)
	}
}

// 仓储错误原样上抛。
func TestListPropagatesRepoError(t *testing.T) {
	boom := errors.New("boom")
	_, err := newReader(t, &fakeLister{err: boom}).List(context.Background(), appaudit.ListFilter{})
	if !errors.Is(err, boom) {
		t.Fatalf("err = %v, want boom", err)
	}
}
