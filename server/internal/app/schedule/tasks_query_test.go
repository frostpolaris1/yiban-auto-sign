package schedule

// 任务检索用例的单测：替身端口，不打真库。
//
// # 口径
// 只测用例自身的判据：作用域拒绝、业务日与状态词校验、页大小校验、
// 手机号掩码出口。SQL 行为（筛选、游标、作用域谓词）由 cmd/server 的
// UX-3 e2e 对真库覆盖，这里不重复。

import (
	"context"
	"testing"
	"time"

	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// fakeTaskSource 记录入参并回放固定行。
type fakeTaskSource struct {
	got     coreschedule.TaskQueryFilter
	rows    []coreschedule.TaskQueryRow
	next    string
	err     error
	calls   int
	lastKeg corescope.Scope
}

func (f *fakeTaskSource) ListTasks(ctx context.Context, scope corescope.Scope,
	f2 coreschedule.TaskQueryFilter) ([]coreschedule.TaskQueryRow, string, error) {
	f.calls++
	f.lastKeg = scope
	f.got = f2
	if f.err != nil {
		return nil, "", f.err
	}
	return f.rows, f.next, nil
}

func newTaskQueryForTest(src *fakeTaskSource) *TaskQuery {
	return NewTaskQuery(src, fakeClockAt("2026-10-31T10:00:00+08:00"), mustCST())
}

func fakeClockAt(value string) fakeClock {
	at, err := time.Parse(time.RFC3339, value)
	if err != nil {
		panic(err)
	}
	return fakeClock{at: at}
}

type fakeClock struct{ at time.Time }

func (c fakeClock) Now() time.Time { return c.at }

func mustCST() *time.Location {
	// 固定 +08:00：不用 IANA 名，canonical 的 clock.tz 判据不收测试里的字面量。
	return time.FixedZone("CST", 8*3600)
}

func oneRow() coreschedule.TaskQueryRow {
	run := time.Date(2026, 10, 31, 8, 0, 0, 0, time.UTC)
	return coreschedule.TaskQueryRow{
		ID: "t1", AccountID: "a1", Phone: "13800138000",
		SchoolID: "s1", SchoolName: "学校甲", TaskName: "早签到",
		State: string(status.TaskSuccess), Attempts: 1, Result: "ok",
		RunAt: run, UpdatedAt: run,
	}
}

// user 范围没有任务可见面：拒绝，不把空集合当「自己的任务」。
func TestTaskQueryRejectsUserScope(t *testing.T) {
	src := &fakeTaskSource{rows: []coreschedule.TaskQueryRow{oneRow()}}
	q := newTaskQueryForTest(src)
	if _, err := q.List(context.Background(), corescope.User("u1"),
		coreschedule.TaskQueryFilter{Limit: 10}); err == nil {
		t.Fatal("user scope must be rejected, not silently narrowed")
	}
	if src.calls != 0 {
		t.Fatalf("source called %d times, want 0", src.calls)
	}
}

// day 缺省＝业务日今天；非法形状 400；非法状态词 400。
func TestTaskQueryValidatesDayAndState(t *testing.T) {
	src := &fakeTaskSource{}
	q := newTaskQueryForTest(src)

	if _, err := q.List(context.Background(), corescope.Master(),
		coreschedule.TaskQueryFilter{Limit: 10}); err != nil {
		t.Fatalf("default day: %v", err)
	}
	if src.got.Day != "2026-10-31" {
		t.Fatalf("default day = %q, want the clock's business day", src.got.Day)
	}

	if _, err := q.List(context.Background(), corescope.Master(),
		coreschedule.TaskQueryFilter{Day: "31/10/2026", Limit: 10}); err == nil {
		t.Fatal("malformed day must be a validation error")
	}
	if _, err := q.List(context.Background(), corescope.Master(),
		coreschedule.TaskQueryFilter{Day: "2026-02-30", Limit: 10}); err == nil {
		t.Fatal("a nonexistent calendar day must be a validation error")
	}
	if _, err := q.List(context.Background(), corescope.Master(),
		coreschedule.TaskQueryFilter{Day: "2026-10-30", State: "done", Limit: 10}); err == nil {
		t.Fatal("an unknown state word must be a validation error")
	}
	for _, bad := range []int{0, 201, -1} {
		if _, err := q.List(context.Background(), corescope.Master(),
			coreschedule.TaskQueryFilter{Day: "2026-10-30", Limit: bad}); err == nil {
			t.Fatalf("limit %d must be a validation error", bad)
		}
	}
}

// 出口掩码：明文手机号不进视图；result 过自由文本掩码。
func TestTaskQueryMasksAtTheBoundary(t *testing.T) {
	row := oneRow()
	row.Result = "failed: 13800138000 in text"
	src := &fakeTaskSource{rows: []coreschedule.TaskQueryRow{row}}
	q := newTaskQueryForTest(src)

	page, err := q.List(context.Background(), corescope.Master(),
		coreschedule.TaskQueryFilter{Day: "2026-10-31", Limit: 10})
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if page.Items[0].PhoneMasked != "138****8000" {
		t.Fatalf("masked phone = %q, want 138****8000", page.Items[0].PhoneMasked)
	}
	if page.Items[0].Result != "failed: 138****8000 in text" {
		t.Fatalf("result = %q, want the phone inside masked", page.Items[0].Result)
	}
}

// 导出：上限覆盖页大小；next 非空即截断；有效日回传给调用方。
func TestTaskQueryExportCap(t *testing.T) {
	rows := make([]coreschedule.TaskQueryRow, 0, ExportCap)
	for i := 0; i < ExportCap; i++ {
		rows = append(rows, oneRow())
	}
	src := &fakeTaskSource{rows: rows, next: "more"}
	q := newTaskQueryForTest(src)

	got, err := q.ExportAll(context.Background(), corescope.Master(),
		coreschedule.TaskQueryFilter{})
	if err != nil {
		t.Fatalf("export: %v", err)
	}
	if len(got.Rows) != ExportCap || !got.Truncated {
		t.Fatalf("rows=%d truncated=%v, want %d true", len(got.Rows), got.Truncated, ExportCap)
	}
	if src.got.Limit != ExportCap {
		t.Fatalf("source limit = %d, want the export cap", src.got.Limit)
	}
	if got.Day != "2026-10-31" {
		t.Fatalf("export day = %q, want the default business day", got.Day)
	}

	src.next = ""
	got, err = q.ExportAll(context.Background(), corescope.Master(),
		coreschedule.TaskQueryFilter{})
	if err != nil {
		t.Fatalf("export 2: %v", err)
	}
	if got.Truncated {
		t.Fatal("an exhausted cursor must not read as truncated")
	}
}

// q 的检索面：11 位全号才进 Phone 精确匹配；前缀与非号码只走名称子串。
func TestTaskQueryClassifiesQuery(t *testing.T) {
	src := &fakeTaskSource{}
	q := newTaskQueryForTest(src)

	cases := []struct {
		name      string
		query     string
		wantPhone string
	}{
		{"full phone", "13800138001", "13800138001"},
		{"short prefix", "1380013801", ""},
		{"wrong leading digit", "23800138001", ""},
		{"name text", "甲校", ""},
		{"twelve digits not a phone", "138001380011", ""},
		{"second digit below 3", "12800138017", ""},
		{"second digit is 0", "10800138017", ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if _, err := q.List(context.Background(), corescope.Master(),
				coreschedule.TaskQueryFilter{Day: "2026-10-31", Query: c.query, Limit: 10}); err != nil {
				t.Fatalf("list: %v", err)
			}
			if src.got.Phone != c.wantPhone {
				t.Fatalf("phone = %q, want %q", src.got.Phone, c.wantPhone)
			}
			if src.got.Query != c.query {
				t.Fatalf("query passthrough = %q, want %q", src.got.Query, c.query)
			}
		})
	}
}
