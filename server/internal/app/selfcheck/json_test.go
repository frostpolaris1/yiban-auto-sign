package selfcheck

// --json 结构 golden。
//
// # 口径
// golden 用固定时钟、固定构建信息与固定替身环境生成；
// 任何字段增删或顺序变化都会让本用例红，迫使 U2 先改 schema 版本。

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

var updateGolden = flag.Bool("update", false, "rewrite testdata/report.golden.json")

// goldenReport 构造混合结果：PASS、FAIL、SKIP 三态都出现。
func goldenReport(t *testing.T) Report {
	t.Helper()
	f := newFixture()
	f.opts.UIDir = ""
	delete(f.fs.dirs, "/var/lib/yiban/photos")
	delete(f.fs.dirs, DefaultBackupDir)
	f.deps.LookPath = func(name string) (string, error) {
		if name == "pg_restore" {
			return "", os.ErrNotExist
		}
		return "/usr/bin/" + name, nil
	}
	f.db.version = 21
	f.embedded = 22
	other := f.opts
	other.HashPepper = strings.Repeat("f", 32)
	if _, err := RecordKeys(f.fs, f.opts.StatePath, other, fixedNow); err != nil {
		t.Fatalf("RecordKeys: %v", err)
	}
	f.deps.ServiceProbe = func(context.Context) (bool, bool, string) { return true, false, "inactive" }
	return f.run(t)
}

func TestJSONReportGolden(t *testing.T) {
	rep := goldenReport(t)
	body, err := rep.JSON()
	if err != nil {
		t.Fatalf("JSON: %v", err)
	}
	path := filepath.Join("testdata", "report.golden.json")
	if *updateGolden {
		if err := os.MkdirAll("testdata", 0o755); err != nil {
			t.Fatalf("mkdir testdata: %v", err)
		}
		if err := os.WriteFile(path, body, 0o644); err != nil {
			t.Fatalf("write golden: %v", err)
		}
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read golden (run go test -run TestJSONReportGolden -update): %v", err)
	}
	if !bytes.Equal(body, want) {
		t.Fatalf("JSON report drifted from golden.\n--- got ---\n%s\n--- want ---\n%s", body, want)
	}
}

func TestJSONReportShape(t *testing.T) {
	rep := goldenReport(t)
	body, err := rep.JSON()
	if err != nil {
		t.Fatalf("JSON: %v", err)
	}
	var doc struct {
		SchemaVersion int    `json:"schema_version"`
		GeneratedAt   string `json:"generated_at"`
		Build         struct {
			Version   string `json:"version"`
			Commit    string `json:"commit"`
			BuildTime string `json:"build_time"`
		} `json:"build"`
		Checks []struct {
			Name   string `json:"name"`
			Status string `json:"status"`
			Detail string `json:"detail"`
			Hint   string `json:"hint"`
		} `json:"checks"`
		Summary struct {
			Pass     int `json:"pass"`
			Fail     int `json:"fail"`
			Skip     int `json:"skip"`
			ExitCode int `json:"exit_code"`
		} `json:"summary"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		t.Fatalf("decode JSON: %v", err)
	}
	if doc.SchemaVersion != ReportSchemaVersion {
		t.Fatalf("schema_version = %d, want %d", doc.SchemaVersion, ReportSchemaVersion)
	}
	if doc.GeneratedAt != "2026-11-04T12:00:00Z" {
		t.Fatalf("generated_at = %q", doc.GeneratedAt)
	}
	if doc.Build.Version != "0.7.0" || doc.Build.Commit != "abc1234" {
		t.Fatalf("build = %+v", doc.Build)
	}
	if len(doc.Checks) != len(wantCheckNames) {
		t.Fatalf("checks = %d, want %d", len(doc.Checks), len(wantCheckNames))
	}
	if doc.Summary.Pass+doc.Summary.Fail+doc.Summary.Skip != len(doc.Checks) {
		t.Fatalf("summary does not add up: %+v over %d checks", doc.Summary, len(doc.Checks))
	}
	if doc.Summary.ExitCode != 1 {
		t.Fatalf("exit_code = %d, want 1", doc.Summary.ExitCode)
	}
	for _, c := range doc.Checks {
		switch c.Status {
		case string(StatusPass), string(StatusFail), string(StatusSkip):
		default:
			t.Fatalf("check %s has unknown status %q", c.Name, c.Status)
		}
		if c.Detail == "" {
			t.Fatalf("check %s has empty detail", c.Name)
		}
	}
	assertNoSecrets(t, string(body))
}

func TestJSONIsOnlyOutput(t *testing.T) {
	body, err := goldenReport(t).JSON()
	if err != nil {
		t.Fatalf("JSON: %v", err)
	}
	text := string(body)
	for _, human := range []string{"[ OK ]", "[FAIL]", "[SKIP]", "汇总：", "selfcheck: version="} {
		if strings.Contains(text, human) {
			t.Fatalf("JSON contains human output %q", human)
		}
	}
}
