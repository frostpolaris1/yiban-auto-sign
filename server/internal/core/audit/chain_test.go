package audit_test

import (
	"bytes"
	"encoding/hex"
	"errors"
	"go/ast"
	"go/parser"
	"go/token"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
)

// TestEncodeDetailRejectsPersonalData 钉住 schema §8.5 的 detail 规则。
//
// 每一行都是一种**绝不可进审计表**的形状。
// 清单覆盖 W2A 复审报告的缺陷：词后跟手机号、数字型手机号、
// rows 内手机号、嵌套 map。
func TestEncodeDetailRejectsPersonalData(t *testing.T) {
	reason201 := strings.Repeat("x", 201)
	for _, tc := range []struct {
		name   string
		detail map[string]any
		want   error
	}{
		{"bare phone", map[string]any{"reason": "13800138000"}, coreaudit.ErrDetailPII},
		{"separated phone", map[string]any{"reason": "139-0013-9000"}, coreaudit.ErrDetailPII},
		{"phone with the country code", map[string]any{"reason": "+86 139 0013 9000"}, coreaudit.ErrDetailPII},
		{"phone after a word", map[string]any{"reason": "user13800138000"}, coreaudit.ErrDetailPII},
		{"phone before a word", map[string]any{"reason": "13800138000abc"}, coreaudit.ErrDetailPII},
		{"bare email", map[string]any{"reason": "a@b.com"}, coreaudit.ErrDetailPII},
		{"email after a word", map[string]any{"reason": "user@example.com"}, coreaudit.ErrDetailPII},
		{"non-ASCII email", map[string]any{"reason": "退回 学生@example.com"}, coreaudit.ErrDetailPII},
		{"integer phone", map[string]any{"count": int64(13800138000)}, coreaudit.ErrDetailPII},
		{"float phone", map[string]any{"count": float64(13800138000)}, coreaudit.ErrDetailPII},
		{"phone in rows", map[string]any{"rows": []int{1, 13800138000}}, coreaudit.ErrDetailPII},
		{"phone in a nested map", map[string]any{"from": map[string]any{"v": "13800138000"}}, coreaudit.ErrDetailType},
		{"map value", map[string]any{"field": map[string]any{"v": "status"}}, coreaudit.ErrDetailType},
		{"untyped slice", map[string]any{"count": []any{int64(1)}}, coreaudit.ErrDetailType},
		{"rows outside the rows key", map[string]any{"field": []int{1}}, coreaudit.ErrDetailType},
		{"reason over 200 characters", map[string]any{"reason": reason201}, coreaudit.ErrDetailReason},
		{"trace id that is a bare phone", map[string]any{"trace_id": "13800138000"}, coreaudit.ErrDetailShape},
		{"trace id with a dashed phone", map[string]any{"trace_id": "138-0013-8000"}, coreaudit.ErrDetailShape},
		{"trace id req run over the counter width", map[string]any{"trace_id": "req-13800138000"}, coreaudit.ErrDetailShape},
		{"trace id req at the counter cap plus one", map[string]any{"trace_id": "req-1000000"}, coreaudit.ErrDetailShape},
		{"trace id with an illegal character", map[string]any{"trace_id": "abc@def"}, coreaudit.ErrDetailShape},
		{"trace id over 64 characters", map[string]any{"trace_id": strings.Repeat("a", 65)}, coreaudit.ErrDetailShape},
		{"kid with upper case", map[string]any{"kid": "0123456789ABCDEF"}, coreaudit.ErrDetailShape},
		{"kid of the wrong length", map[string]any{"kid": "0123456789abcde"}, coreaudit.ErrDetailShape},
		{"field with spaces", map[string]any{"field": "user name"}, coreaudit.ErrDetailShape},
		{"from with a dash", map[string]any{"from": "not-a-status"}, coreaudit.ErrDetailShape},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, err := coreaudit.EncodeDetail(tc.detail)
			if err == nil {
				t.Fatalf("detail %v was accepted; want %v", tc.detail, tc.want)
			}
			if !errors.Is(err, tc.want) {
				t.Fatalf("detail %v: got %v; want %v", tc.detail, err, tc.want)
			}
		})
	}
}

// TestEncodeDetailAcceptsScalars 钉住被接受的形状。
func TestEncodeDetailAcceptsScalars(t *testing.T) {
	var nilReason *string
	got, err := coreaudit.EncodeDetail(map[string]any{
		"field":    "status",
		"from":     "pending",
		"to":       "active",
		"reason":   "photo rejected",
		"count":    int64(3),
		"kid":      "0123456789abcdef",
		"rows":     []int{1, 2},
		"trace_id": "0f2e1d3c4b5a69788796a5b4c3d2e1f0",
	})
	if err != nil {
		t.Fatalf("valid detail rejected: %v", err)
	}
	// encoding/json 对 map 键排序，故字节是规范的。
	const want = `{"count":3,"field":"status","from":"pending","kid":"0123456789abcdef",` +
		`"reason":"photo rejected","rows":[1,2],"to":"active","trace_id":"0f2e1d3c4b5a69788796a5b4c3d2e1f0"}`
	if got != want {
		t.Fatalf("canonical detail:\n got %s\nwant %s", got, want)
	}

	if _, err := coreaudit.EncodeDetail(map[string]any{"reason": nilReason}); err != nil {
		t.Fatalf("a nil pointer must be accepted as null: %v", err)
	}
	// 随机 32 位十六进制请求 id 必须通过，即使其中含 11 位数字。
	if _, err := coreaudit.EncodeDetail(map[string]any{"trace_id": "06317272289944f6e935132eb62042ec"}); err != nil {
		t.Fatalf("a 32-hex trace id must be accepted: %v", err)
	}
	// req-<计数> 兜底形式最多六位数字，即路由实际可生成的最大宽度。
	for _, id := range []string{"req-1", "req-999999"} {
		if _, err := coreaudit.EncodeDetail(map[string]any{"trace_id": id}); err != nil {
			t.Fatalf("fallback id %s must be accepted: %v", id, err)
		}
	}
	if _, err := coreaudit.EncodeDetail(nil); err != nil {
		t.Fatalf("an empty detail must be accepted: %v", err)
	}
}

// 下面两个常量是回归钉，不是自检。
// 改动 payload 布局或 formatTime，会让历史链的存储哈希与复算 payload 不符。
// 这种改动之后重算常量，**只有配套链迁移方案才算正确**。
const (
	wantPayloadHex = "796962616e2e61756469742e76310000000020000000000000000000000000000000000000000000000000000000000000000000000018323032362d31302d32355431323a33343a35362e3738395a0000002430313932663066302d303030302d373030302d383030302d3030303030303030303030310000002430313932663066302d303030302d373030302d383030302d3030303030303030303030320000000561646d696e00000010612a2a2a406578616d706c652e636f6d000000166163636f756e742e7265766965775f617070726f7665000000076163636f756e740000002430313932663066302d303030302d373030302d383030302d303030303030303030303033000000317b226669656c64223a22737461747573222c2266726f6d223a2270656e64696e67222c22746f223a22616374697665227d"
	wantHashHex    = "6f997b8cfdec535c9db3648e694adfdd8268aae6b3050f722d3657ebc0c43ef5"
)

// fixedRow 是冻结样本。
// 每个 payload 字段都非空，Payload 漏掉任一字段都会改变字节。
func fixedRow() coreaudit.Row {
	return coreaudit.Row{
		ID:          7,
		TS:          time.Date(2026, 10, 25, 12, 34, 56, 789000000, time.UTC),
		SchoolID:    "0192f0f0-0000-7000-8000-000000000001",
		ActorUserID: "0192f0f0-0000-7000-8000-000000000002",
		ActorRole:   "admin",
		ActorLabel:  "a***@example.com",
		Action:      "account.review_approve",
		TargetType:  "account",
		TargetID:    "0192f0f0-0000-7000-8000-000000000003",
		Detail:      `{"field":"status","from":"pending","to":"active"}`,
		PrevHash:    coreaudit.ZeroHash(),
	}
}

// TestPayloadBytesAreFixed 钉住 payload 的精确字节与 HMAC 输出。
func TestPayloadBytesAreFixed(t *testing.T) {
	row := fixedRow()
	if got := hex.EncodeToString(coreaudit.Payload(row)); got != wantPayloadHex {
		t.Fatalf("payload bytes changed; got %s", got)
	}
	key := []byte("0123456789abcdef0123456789abcdef")
	if got := hex.EncodeToString(coreaudit.Hash(key, row)); got != wantHashHex {
		t.Fatalf("hash bytes changed; got %s", got)
	}
}

// TestPayloadSeparatesFields 守卫长度前缀分帧：
// 在相邻两字段之间移位一个字节，payload 必须改变。
func TestPayloadSeparatesFields(t *testing.T) {
	a := fixedRow()
	b := fixedRow()
	b.TargetType = "accoun"
	b.TargetID = "t0182f0f0-0000-7000-8000-000000000003"
	if bytes.Equal(coreaudit.Payload(a), coreaudit.Payload(b)) {
		t.Fatal("payload is ambiguous across field boundaries")
	}
}

// TestActionVocabularyMatchesDatabaseShape 逐条核对 Action 常量与
// audit_logs 表的 CHECK 形状。
//
// 正则取自 migrations/postgres/00007_audit_logs.sql 的 ck_audit_action_shape。
// 常量从源文件读，故新增动作无需改本测试。
func TestActionVocabularyMatchesDatabaseShape(t *testing.T) {
	// 必须与 migrations/postgres/00007_audit_logs.sql 保持一致。
	shape := regexp.MustCompile(`^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$`)
	actions := actionConstants(t, "chain.go")
	if len(actions) < 20 {
		t.Fatalf("found %d action constants; the scan is probably wrong", len(actions))
	}
	seen := map[string]bool{}
	for _, action := range actions {
		if !shape.MatchString(action) {
			t.Errorf("action %q does not match the database CHECK shape", action)
		}
		if seen[action] {
			t.Errorf("action %q is declared twice", action)
		}
		seen[action] = true
	}
}

// actionConstants 读出 path 中 Action 常量的字符串值。
func actionConstants(t *testing.T, path string) []string {
	t.Helper()
	file, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
	if err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	var out []string
	for _, decl := range file.Decls {
		gen, ok := decl.(*ast.GenDecl)
		if !ok || gen.Tok != token.CONST {
			continue
		}
		for _, spec := range gen.Specs {
			value, ok := spec.(*ast.ValueSpec)
			if !ok {
				continue
			}
			ident, ok := value.Type.(*ast.Ident)
			if !ok || ident.Name != "Action" {
				continue
			}
			for _, expr := range value.Values {
				lit, ok := expr.(*ast.BasicLit)
				if !ok || lit.Kind != token.STRING {
					continue
				}
				text, err := strconv.Unquote(lit.Value)
				if err != nil {
					t.Fatalf("unquote action literal %s: %v", lit.Value, err)
				}
				out = append(out, text)
			}
		}
	}
	return out
}

// 审计守卫用检测口径：纪元毫秒时间戳放行，分隔手机号与邮箱拒绝。
func TestEncodeDetailUsesDetectionCaliber(t *testing.T) {
	if _, err := coreaudit.EncodeDetail(map[string]any{"reason": "订单 1735689600000 已核对"}); err != nil {
		t.Fatalf("时间戳形态被拒绝: %v", err)
	}
	if _, err := coreaudit.EncodeDetail(map[string]any{"reason": "退回 139-0013-9000"}); err == nil {
		t.Fatal("分隔手机号未被拒绝")
	}
	if _, err := coreaudit.EncodeDetail(map[string]any{"reason": "a@b.com"}); err == nil {
		t.Fatal("邮箱未被拒绝")
	}
}

// RFC3339Nano 的「秒.纳秒」会拼出 11 位数字，但不是手机号。
func TestEncodeDetailAllowsTimestampFraction(t *testing.T) {
	if _, err := coreaudit.EncodeDetail(
		map[string]any{"reason": "2026-10-26T13:10:16.429881512+08:00"}); err != nil {
		t.Fatalf("时间戳被拒绝: %v", err)
	}
}

// P5-C location.set 的两个新键：
// mode 是状态词；has_reason 只接受布尔，字符串即拒绝。
func TestEncodeDetailAcceptsLocationSetKeys(t *testing.T) {
	if _, err := coreaudit.EncodeDetail(map[string]any{"mode": "fixed", "has_reason": true}); err != nil {
		t.Fatalf("mode+has_reason 被拒绝: %v", err)
	}
	if _, err := coreaudit.EncodeDetail(map[string]any{"mode": "GEOGRAPHY-POINT"}); err == nil {
		t.Fatal("mode 非状态词未被拒绝")
	}
	if _, err := coreaudit.EncodeDetail(map[string]any{"has_reason": "yes"}); err == nil {
		t.Fatal("has_reason 字符串未被拒绝")
	}
}

// R4A：段内连写的手机号不得进审计链；掩码后的文本必须通过。
func TestEncodeDetailRejectsFusedPhone(t *testing.T) {
	const raw = "1380013800013800138001"
	if _, err := coreaudit.EncodeDetail(map[string]any{"reason": raw}); !errors.Is(err, coreaudit.ErrDetailPII) {
		t.Fatalf("原始连写号码 err = %v, want ErrDetailPII", err)
	}
	masked := mask.MaskText(raw)
	if masked == raw {
		t.Fatalf("MaskText 未改写: %q", masked)
	}
	if _, err := coreaudit.EncodeDetail(map[string]any{"reason": masked}); err != nil {
		t.Fatalf("掩码后 err = %v, want nil (%q)", err, masked)
	}
}
