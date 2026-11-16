package schedule_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	appstats "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/stats"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
)

type fakeFailureStats struct {
	fakeStats
	report     appstats.FailureReport
	err        error
	calls      int
	lastScope  corescope.Scope
	lastFilter coreschedule.SignFailureFilter
}

func (f *fakeFailureStats) Failures(_ context.Context, scope corescope.Scope, filter coreschedule.SignFailureFilter) (appstats.FailureReport, error) {
	f.calls++
	f.lastScope = scope
	f.lastFilter = filter
	if f.err != nil {
		return appstats.FailureReport{}, f.err
	}
	rep := f.report
	if rep.Day == "" {
		rep.Day = filter.Day
	}
	return rep, nil
}

const (
	failTestSchoolID = "01900000-0000-7000-8000-000000000001"
	failTestTaskID   = "01900000-0000-7000-8000-000000000101"
)

func TestSignStatsFailuresTable(t *testing.T) {
	cases := []struct {
		name       string
		path       string
		scope      corescope.Scope
		unauth     bool
		nilStats   bool
		noFailures bool
		stats      *fakeFailureStats
		wantStatus int
		checkBody  func(t *testing.T, body oapi.SignFailuresReport, stats *fakeFailureStats)
	}{
		{
			name:       "unauthenticated returns 401",
			path:       "/api/sign-stats/failures",
			scope:      corescope.Master(),
			unauth:     true,
			wantStatus: http.StatusUnauthorized,
		},
		{
			name:       "user role returns 403",
			path:       "/api/sign-stats/failures",
			scope:      corescope.User("u1"),
			wantStatus: http.StatusForbidden,
		},
		{
			name:       "nil stats returns 500",
			path:       "/api/sign-stats/failures",
			scope:      corescope.Master(),
			nilStats:   true,
			wantStatus: http.StatusInternalServerError,
		},
		{
			name:       "stats missing FailureStats capability returns 500",
			path:       "/api/sign-stats/failures",
			scope:      corescope.Master(),
			noFailures: true,
			wantStatus: http.StatusInternalServerError,
		},
		{
			name:       "default day uses configured business day and empty items",
			path:       "/api/sign-stats/failures",
			scope:      corescope.Master(),
			stats:      &fakeFailureStats{},
			wantStatus: http.StatusOK,
			checkBody: func(t *testing.T, body oapi.SignFailuresReport, stats *fakeFailureStats) {
				if stats.lastFilter.Day != "2026-10-29" {
					t.Fatalf("day passed to service = %q, want 2026-10-29", stats.lastFilter.Day)
				}
				if body.Day.Format("2006-01-02") != "2026-10-29" || body.Total != 0 || len(body.Items) != 0 {
					t.Fatalf("body = %+v", body)
				}
			},
		},
		{
			name:       "empty query params treated as omitted",
			path:       "/api/sign-stats/failures?day=&school_id=&task_id=",
			scope:      corescope.Master(),
			stats:      &fakeFailureStats{},
			wantStatus: http.StatusOK,
			checkBody: func(t *testing.T, body oapi.SignFailuresReport, stats *fakeFailureStats) {
				if stats.lastFilter.Day != "2026-10-29" || stats.lastFilter.SchoolID != "" || stats.lastFilter.TaskID != "" {
					t.Fatalf("filter = %+v, want default day and empty filters", stats.lastFilter)
				}
				if body.Scope.SchoolId != nil || body.Scope.TaskId != nil {
					t.Fatalf("scope = %+v, want nil fields", body.Scope)
				}
			},
		},
		{
			name:  "passes filters and maps items and total",
			path:  "/api/sign-stats/failures?day=2026-10-28&school_id=" + failTestSchoolID + "&task_id=" + failTestTaskID,
			scope: corescope.Admin([]string{failTestSchoolID}),
			stats: &fakeFailureStats{
				report: appstats.FailureReport{
					Day:      "2026-10-28",
					SchoolID: failTestSchoolID,
					TaskID:   failTestTaskID,
					Items: []coreschedule.SignFailureBucket{
						{Result: "credential_invalid", Count: 3},
						{Result: "unknown", Count: 1},
					},
					Total: 4,
				},
			},
			wantStatus: http.StatusOK,
			checkBody: func(t *testing.T, body oapi.SignFailuresReport, stats *fakeFailureStats) {
				if stats.lastFilter.Day != "2026-10-28" ||
					stats.lastFilter.SchoolID != failTestSchoolID ||
					stats.lastFilter.TaskID != failTestTaskID {
					t.Fatalf("filter = %+v", stats.lastFilter)
				}
				if body.Total != 4 || len(body.Items) != 2 {
					t.Fatalf("body = %+v", body)
				}
				if body.Scope.SchoolId == nil || *body.Scope.SchoolId != failTestSchoolID ||
					body.Scope.TaskId == nil || *body.Scope.TaskId != failTestTaskID {
					t.Fatalf("scope = %+v", body.Scope)
				}
			},
		},
		{
			name:       "validation error maps to 400",
			path:       "/api/sign-stats/failures?day=bad-day",
			scope:      corescope.Master(),
			stats:      &fakeFailureStats{err: errs.New(errs.CodeValidation, "day must be YYYY-MM-DD")},
			wantStatus: http.StatusBadRequest,
		},
		{
			name:       "cross-school not_found maps to 404",
			path:       "/api/sign-stats/failures?school_id=" + failTestSchoolID,
			scope:      corescope.Admin(nil),
			stats:      &fakeFailureStats{err: errs.New(errs.CodeNotFound, "school not found")},
			wantStatus: http.StatusNotFound,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var engine http.Handler
			var issued coreauth.Issued
			switch {
			case tc.nilStats:
				engine, issued = newRouterWithStats(t, tc.scope, &fakePlanner{}, &fakeTimePrefs{}, nil)
			case tc.noFailures:
				engine, issued = newRouterWithStats(t, tc.scope, &fakePlanner{}, &fakeTimePrefs{}, &fakeStats{})
			default:
				engine, issued = newRouterWithStats(t, tc.scope, &fakePlanner{}, &fakeTimePrefs{}, tc.stats)
			}

			var rec *httptest.ResponseRecorder
			if tc.unauth {
				req := httptest.NewRequest(http.MethodGet, tc.path, nil)
				rec = httptest.NewRecorder()
				engine.ServeHTTP(rec, req)
			} else {
				rec = do(t, engine, issued, http.MethodGet, tc.path, nil)
			}
			if rec.Code != tc.wantStatus {
				t.Fatalf("status = %d, want %d; body=%s", rec.Code, tc.wantStatus, rec.Body.String())
			}
			if tc.wantStatus == http.StatusOK && tc.checkBody != nil {
				var raw map[string]json.RawMessage
				if err := json.Unmarshal(rec.Body.Bytes(), &raw); err != nil {
					t.Fatalf("decode raw: %v", err)
				}
				if strings.TrimSpace(string(raw["items"])) == "null" {
					t.Fatalf("items must be JSON array, never null: %s", rec.Body.String())
				}
				var body oapi.SignFailuresReport
				if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
					t.Fatalf("decode: %v", err)
				}
				tc.checkBody(t, body, tc.stats)
			}
		})
	}
}
