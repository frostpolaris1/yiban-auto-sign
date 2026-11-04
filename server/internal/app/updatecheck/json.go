// selfcheck 的 JSON 报告形态。
//
// # 口径
// 结构稳定、字段名固定；U2 按 schema_version 与 check.name 消费。
// --json 只输出这一份 JSON，不混人读行。
package updatecheck

import (
	"encoding/json"
	"time"
)

// jsonBuild 是报告里的版本身份。
type jsonBuild struct {
	Version   string `json:"version"`
	Commit    string `json:"commit"`
	BuildTime string `json:"build_time"`
}

// jsonCheck 是一条检查的序列化形态。
type jsonCheck struct {
	Name   string `json:"name"`
	Status string `json:"status"`
	Detail string `json:"detail"`
	Hint   string `json:"hint,omitempty"`
}

// jsonSummary 是计数与退出码。
type jsonSummary struct {
	Pass     int `json:"pass"`
	Fail     int `json:"fail"`
	Skip     int `json:"skip"`
	ExitCode int `json:"exit_code"`
}

// jsonReport 是 --json 的顶层结构。
type jsonReport struct {
	SchemaVersion int         `json:"schema_version"`
	GeneratedAt   string      `json:"generated_at"`
	Build         jsonBuild   `json:"build"`
	Checks        []jsonCheck `json:"checks"`
	Summary       jsonSummary `json:"summary"`
}

// JSON 渲染 --json 报告（缩进两格，结尾换行）。
func (r Report) JSON() ([]byte, error) {
	doc := jsonReport{
		SchemaVersion: r.SchemaVersion,
		GeneratedAt:   r.GeneratedAt.UTC().Format(time.RFC3339),
		Build: jsonBuild{
			Version:   r.Build.Version,
			Commit:    r.Build.Commit,
			BuildTime: r.Build.BuildTime,
		},
		Checks: make([]jsonCheck, 0, len(r.Checks)),
		Summary: jsonSummary{
			Pass:     r.Passed(),
			Fail:     r.Failed(),
			Skip:     r.Skipped(),
			ExitCode: r.ExitCode(),
		},
	}
	for _, c := range r.Checks {
		doc.Checks = append(doc.Checks, jsonCheck{
			Name:   c.Name,
			Status: string(c.Status),
			Detail: c.Detail,
			Hint:   c.Hint,
		})
	}
	body, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return nil, err
	}
	return append(body, '\n'), nil
}
