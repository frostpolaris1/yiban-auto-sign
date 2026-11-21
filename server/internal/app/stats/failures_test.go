package stats_test

import (
	"context"
	"testing"

	appstats "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/stats"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

type fakeStore struct {
	schools map[string]bool
	tasks   map[string]string
	buckets []coreschedule.SignFailureBucket
	err     error

	calls      int
	lastScope  corescope.Scope
	lastFilter coreschedule.SignFailureFilter
}

func (f *fakeStore) SignStats(context.Context, corescope.Scope, string, string) (coreschedule.SignStats, error) {
	return coreschedule.SignStats{}, nil
}

func (f *fakeStore) OffsetCounts(context.Context, string, string, string) (map[int]int, error) {
	return nil, nil
}

func (f *fakeStore) SchoolExists(_ context.Context, schoolID string) (bool, error) {
	if f.err != nil {
		return false, f.err
	}
	return f.schools[schoolID], nil
}

func (f *fakeStore) TaskSchoolID(_ context.Context, taskID string) (string, error) {
	if f.err != nil {
		return "", f.err
	}
	schoolID, ok := f.tasks[taskID]
	if !ok {
		return "", errs.New(errs.CodeNotFound, "school task not found")
	}
	return schoolID, nil
}

func (f *fakeStore) FailureCounts(_ context.Context, scope corescope.Scope, filter coreschedule.SignFailureFilter) ([]coreschedule.SignFailureBucket, error) {
	f.calls++
	f.lastScope = scope
	f.lastFilter = filter
	if f.err != nil {
		return nil, f.err
	}
	return f.buckets, nil
}

const (
	testSchoolA      = "01900000-0000-7000-8000-00000000000a"
	testSchoolAUpper = "01900000-0000-7000-8000-00000000000A"
	testSchoolB      = "01900000-0000-7000-8000-00000000000b"
	testTaskA1       = "01900000-0000-7000-8000-00000000010a"
	testTaskA1Upper  = "01900000-0000-7000-8000-00000000010A"
	testTaskB1       = "01900000-0000-7000-8000-00000000020b"
)

func TestServiceFailuresNormalizationAndSort(t *testing.T) {
	store := &fakeStore{
		schools: map[string]bool{testSchoolA: true},
		tasks:   map[string]string{testTaskA1: testSchoolA},
		buckets: []coreschedule.SignFailureBucket{
			{Result: "", Count: 2},
			{Result: "   ", Count: 1},
			{Result: "unknown", Count: 1},
			{Result: "custom_drift", Count: 3},
			{Result: "drift_13800000040", Count: 1},
			{Result: "blocked", Count: 3},
			{Result: "credential_invalid", Count: 5},
		},
	}
	svc, err := appstats.NewService(store)
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	report, err := svc.Failures(context.Background(), corescope.Master(), coreschedule.SignFailureFilter{
		Day:      "2026-11-16",
		SchoolID: testSchoolA,
		TaskID:   testTaskA1,
	})
	if err != nil {
		t.Fatalf("Failures: %v", err)
	}
	if report.Total != 16 {
		t.Fatalf("total = %d, want 16", report.Total)
	}
	// 排序：count DESC, result ASC；""/"   "/"unknown" 合并成 unknown=4；drift 中手机号脱敏。
	want := []coreschedule.SignFailureBucket{
		{Result: "credential_invalid", Count: 5},
		{Result: "unknown", Count: 4},
		{Result: "blocked", Count: 3},
		{Result: "custom_drift", Count: 3},
		{Result: "drift_138****0040", Count: 1},
	}
	if len(report.Items) != len(want) {
		t.Fatalf("items = %+v, want %+v", report.Items, want)
	}
	for i := range want {
		if report.Items[i] != want[i] {
			t.Fatalf("item[%d] = %+v, want %+v", i, report.Items[i], want[i])
		}
	}
}

func TestServiceFailuresNormalizesUppercaseUUIDForAdminScope(t *testing.T) {
	store := &fakeStore{
		schools: map[string]bool{testSchoolA: true},
		tasks:   map[string]string{testTaskA1: testSchoolA},
		buckets: []coreschedule.SignFailureBucket{{Result: "blocked", Count: 2}},
	}
	svc, err := appstats.NewService(store)
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	report, err := svc.Failures(context.Background(), corescope.Admin([]string{testSchoolA}), coreschedule.SignFailureFilter{
		Day:      "2026-11-16",
		SchoolID: testSchoolAUpper,
		TaskID:   testTaskA1Upper,
	})
	if err != nil {
		t.Fatalf("Failures(uppercase UUID): %v", err)
	}
	if report.SchoolID != testSchoolA || report.TaskID != testTaskA1 || report.Total != 2 {
		t.Fatalf("report = %+v, want normalized lowercase UUIDs and total=2", report)
	}
}

func TestServiceFailuresMissingAndCrossScope404AreIdentical(t *testing.T) {
	store := &fakeStore{
		schools: map[string]bool{testSchoolA: true, testSchoolB: true},
		tasks:   map[string]string{testTaskB1: testSchoolB},
	}
	svc, err := appstats.NewService(store)
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	_, crossTaskErr := svc.Failures(context.Background(), corescope.Admin([]string{testSchoolA}), coreschedule.SignFailureFilter{
		Day:    "2026-11-16",
		TaskID: testTaskB1,
	})
	_, missingTaskErr := svc.Failures(context.Background(), corescope.Master(), coreschedule.SignFailureFilter{
		Day:    "2026-11-16",
		TaskID: "01900000-0000-7000-8000-000000000999",
	})
	if errs.From(crossTaskErr).Code != errs.CodeNotFound ||
		errs.From(missingTaskErr).Code != errs.CodeNotFound ||
		errs.From(crossTaskErr).Message != errs.From(missingTaskErr).Message {
		t.Fatalf("task 404 differs: cross=%v missing=%v", crossTaskErr, missingTaskErr)
	}
}

func TestServiceFailuresScopeAndValidation(t *testing.T) {
	cases := []struct {
		name     string
		scope    corescope.Scope
		filter   coreschedule.SignFailureFilter
		wantCode errs.Code
	}{
		{
			name:     "invalid scope",
			scope:    corescope.Scope{},
			filter:   coreschedule.SignFailureFilter{Day: "2026-11-16"},
			wantCode: errs.CodeInternal,
		},
		{
			name:     "user forbidden",
			scope:    corescope.User("u1"),
			filter:   coreschedule.SignFailureFilter{Day: "2026-11-16"},
			wantCode: errs.CodeForbidden,
		},
		{
			name:     "empty day",
			scope:    corescope.Master(),
			filter:   coreschedule.SignFailureFilter{},
			wantCode: errs.CodeValidation,
		},
		{
			name:     "invalid day",
			scope:    corescope.Master(),
			filter:   coreschedule.SignFailureFilter{Day: "2026-02-30"},
			wantCode: errs.CodeValidation,
		},
		{
			name:     "invalid school_id",
			scope:    corescope.Master(),
			filter:   coreschedule.SignFailureFilter{Day: "2026-11-16", SchoolID: "bad"},
			wantCode: errs.CodeValidation,
		},
		{
			name:     "invalid task_id",
			scope:    corescope.Master(),
			filter:   coreschedule.SignFailureFilter{Day: "2026-11-16", TaskID: "bad"},
			wantCode: errs.CodeValidation,
		},
		{
			name:     "admin cross-school school_id",
			scope:    corescope.Admin([]string{testSchoolA}),
			filter:   coreschedule.SignFailureFilter{Day: "2026-11-16", SchoolID: testSchoolB},
			wantCode: errs.CodeNotFound,
		},
		{
			name:     "admin cross-school task_id",
			scope:    corescope.Admin([]string{testSchoolA}),
			filter:   coreschedule.SignFailureFilter{Day: "2026-11-16", TaskID: testTaskB1},
			wantCode: errs.CodeNotFound,
		},
		{
			name:     "admin cross-school task_id with own school_id hides mismatch",
			scope:    corescope.Admin([]string{testSchoolA}),
			filter:   coreschedule.SignFailureFilter{Day: "2026-11-16", SchoolID: testSchoolA, TaskID: testTaskB1},
			wantCode: errs.CodeNotFound,
		},
		{
			name:     "same-scope mismatch school_id and task_id",
			scope:    corescope.Admin([]string{testSchoolA, testSchoolB}),
			filter:   coreschedule.SignFailureFilter{Day: "2026-11-16", SchoolID: testSchoolA, TaskID: testTaskB1},
			wantCode: errs.CodeValidation,
		},
		{
			name:     "master missing school",
			scope:    corescope.Master(),
			filter:   coreschedule.SignFailureFilter{Day: "2026-11-16", SchoolID: "01900000-0000-7000-8000-000000000999"},
			wantCode: errs.CodeNotFound,
		},
		{
			name:     "master missing task",
			scope:    corescope.Master(),
			filter:   coreschedule.SignFailureFilter{Day: "2026-11-16", TaskID: "01900000-0000-7000-8000-000000000999"},
			wantCode: errs.CodeNotFound,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			store := &fakeStore{
				schools: map[string]bool{testSchoolA: true, testSchoolB: true},
				tasks:   map[string]string{testTaskA1: testSchoolA, testTaskB1: testSchoolB},
			}
			svc, err := appstats.NewService(store)
			if err != nil {
				t.Fatalf("NewService: %v", err)
			}
			_, err = svc.Failures(context.Background(), tc.scope, tc.filter)
			if err == nil {
				t.Fatalf("expected error %s, got nil", tc.wantCode)
			}
			if got := errs.From(err).Code; got != tc.wantCode {
				t.Fatalf("error code = %s, want %s (%v)", got, tc.wantCode, err)
			}
			if store.calls != 0 {
				t.Fatalf("FailureCounts called %d times on rejected request", store.calls)
			}
		})
	}
}
