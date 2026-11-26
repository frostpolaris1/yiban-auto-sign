package schedule

import (
	"bytes"
	"encoding/csv"
	"testing"
	"time"

	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
)

func TestTasksCSVFormulaInjectionLF(t *testing.T) {
	now := time.Date(2026, 11, 26, 8, 0, 0, 0, time.UTC)
	rows := []appschedule.TaskView{
		{
			ID: "t1", AccountID: "a1", PhoneMasked: "138****0001", SchoolID: "s1",
			SchoolName: "\n=cmd", TaskName: "=cmd", State: "pending",
			Attempts: 0, Result: "", RunAt: now, UpdatedAt: now,
		},
		{
			ID: "t2", AccountID: "a2", PhoneMasked: "138****0002", SchoolID: "s2",
			SchoolName: "一中", TaskName: "", State: "done",
			Attempts: 1, Result: "+SUM(A1)", RunAt: now, UpdatedAt: now,
		},
	}

	raw, err := tasksCSV(rows)
	if err != nil {
		t.Fatalf("tasksCSV: %v", err)
	}
	records, err := csv.NewReader(bytes.NewReader(raw)).ReadAll()
	if err != nil {
		t.Fatalf("parse csv: %v", err)
	}
	if len(records) != 3 {
		t.Fatalf("rows = %d, want 3 (header + 2)", len(records))
	}

	cases := []struct {
		row  int
		col  int
		name string
		want string
	}{
		{1, 4, "school_name LF", "'\n=cmd"},
		{1, 5, "task_name equals", "'=cmd"},
		{2, 4, "school_name chinese", "一中"},
		{2, 5, "task_name empty", ""},
		{2, 8, "result plus", "'+SUM(A1)"},
	}
	for _, tc := range cases {
		if got := records[tc.row][tc.col]; got != tc.want {
			t.Errorf("%s = %q, want %q", tc.name, got, tc.want)
		}
	}
}

func TestTasksCSVFormulaInjectionPrefixes(t *testing.T) {
	now := time.Date(2026, 11, 26, 8, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		name   string
		prefix string
	}{
		{name: "equals", prefix: "="},
		{name: "plus", prefix: "+"},
		{name: "minus", prefix: "-"},
		{name: "at", prefix: "@"},
		{name: "tab", prefix: "\t"},
		{name: "cr", prefix: "\r"},
		{name: "lf", prefix: "\n"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cell := tc.prefix + "=cmd"
			raw, err := tasksCSV([]appschedule.TaskView{{
				ID: "t1", AccountID: "a1", PhoneMasked: "138****0001", SchoolID: "s1",
				SchoolName: cell, TaskName: "plain", State: "pending",
				Attempts: 0, Result: "", RunAt: now, UpdatedAt: now,
			}})
			if err != nil {
				t.Fatalf("tasksCSV: %v", err)
			}
			records, err := csv.NewReader(bytes.NewReader(raw)).ReadAll()
			if err != nil {
				t.Fatalf("parse csv: %v", err)
			}
			if len(records) != 2 {
				t.Fatalf("rows = %d, want 2 (header + 1)", len(records))
			}
			if got, want := records[1][4], "'"+cell; got != want {
				t.Errorf("school_name = %q, want %q", got, want)
			}
			if got := records[1][5]; got != "plain" {
				t.Errorf("task_name = %q, want plain", got)
			}
		})
	}
}
