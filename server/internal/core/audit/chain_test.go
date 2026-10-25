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
)

// TestEncodeDetailRejectsPersonalData pins the detail rules of schema §8.5.
//
// Each row is a shape that must never reach the audit table. The list covers
// the defects reported in the W2A review: a phone number attached to a word,
// a numeric phone, a phone inside rows and a nested map.
func TestEncodeDetailRejectsPersonalData(t *testing.T) {
	reason201 := strings.Repeat("x", 201)
	for _, tc := range []struct {
		name   string
		detail map[string]any
		want   error
	}{
		{"bare phone", map[string]any{"reason": "13800138000"}, coreaudit.ErrDetailPII},
		{"phone after a word", map[string]any{"reason": "user13800138000"}, coreaudit.ErrDetailPII},
		{"phone before a word", map[string]any{"reason": "13800138000abc"}, coreaudit.ErrDetailPII},
		{"bare email", map[string]any{"reason": "a@b.com"}, coreaudit.ErrDetailPII},
		{"email after a word", map[string]any{"reason": "user@example.com"}, coreaudit.ErrDetailPII},
		{"integer phone", map[string]any{"count": int64(13800138000)}, coreaudit.ErrDetailPII},
		{"float phone", map[string]any{"count": float64(13800138000)}, coreaudit.ErrDetailPII},
		{"phone in rows", map[string]any{"rows": []int{1, 13800138000}}, coreaudit.ErrDetailPII},
		{"phone in a nested map", map[string]any{"from": map[string]any{"v": "13800138000"}}, coreaudit.ErrDetailType},
		{"map value", map[string]any{"field": map[string]any{"v": "status"}}, coreaudit.ErrDetailType},
		{"untyped slice", map[string]any{"count": []any{int64(1)}}, coreaudit.ErrDetailType},
		{"rows outside the rows key", map[string]any{"field": []int{1}}, coreaudit.ErrDetailType},
		{"reason over 200 characters", map[string]any{"reason": reason201}, coreaudit.ErrDetailReason},
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

// TestEncodeDetailAcceptsScalars pins the accepted shapes.
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
	// encoding/json sorts map keys, so the bytes are canonical.
	const want = `{"count":3,"field":"status","from":"pending","kid":"0123456789abcdef",` +
		`"reason":"photo rejected","rows":[1,2],"to":"active","trace_id":"0f2e1d3c4b5a69788796a5b4c3d2e1f0"}`
	if got != want {
		t.Fatalf("canonical detail:\n got %s\nwant %s", got, want)
	}

	if _, err := coreaudit.EncodeDetail(map[string]any{"reason": nilReason}); err != nil {
		t.Fatalf("a nil pointer must be accepted as null: %v", err)
	}
	// A random 32-hex request id must pass even when it contains 11 digits.
	if _, err := coreaudit.EncodeDetail(map[string]any{"trace_id": "06317272289944f6e935132eb62042ec"}); err != nil {
		t.Fatalf("a 32-hex trace id must be accepted: %v", err)
	}
	if _, err := coreaudit.EncodeDetail(nil); err != nil {
		t.Fatalf("an empty detail must be accepted: %v", err)
	}
}

// The next two constants are a regression pin, not a self-check. A change to
// the payload layout or to formatTime makes the stored hashes of every
// existing chain disagree with the rebuilt payload. Recomputing the constants
// after such a change is correct only together with a chain migration plan.
const (
	wantPayloadHex = "796962616e2e61756469742e76310000000020000000000000000000000000000000000000000000000000000000000000000000000018323032362d31302d32355431323a33343a35362e3738395a0000002430313932663066302d303030302d373030302d383030302d3030303030303030303030310000002430313932663066302d303030302d373030302d383030302d3030303030303030303030320000000561646d696e00000010612a2a2a406578616d706c652e636f6d000000166163636f756e742e7265766965775f617070726f7665000000076163636f756e740000002430313932663066302d303030302d373030302d383030302d303030303030303030303033000000317b226669656c64223a22737461747573222c2266726f6d223a2270656e64696e67222c22746f223a22616374697665227d"
	wantHashHex    = "6f997b8cfdec535c9db3648e694adfdd8268aae6b3050f722d3657ebc0c43ef5"
)

// fixedRow is the frozen sample. Every payload field is non-empty so a field
// dropped from Payload changes the bytes.
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

// TestPayloadBytesAreFixed pins the exact payload bytes and the HMAC output.
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

// TestPayloadSeparatesFields guards the length-prefix framing: shifting one
// byte between two adjacent fields must change the payload.
func TestPayloadSeparatesFields(t *testing.T) {
	a := fixedRow()
	b := fixedRow()
	b.TargetType = "accoun"
	b.TargetID = "t0182f0f0-0000-7000-8000-000000000003"
	if bytes.Equal(coreaudit.Payload(a), coreaudit.Payload(b)) {
		t.Fatal("payload is ambiguous across field boundaries")
	}
}

// TestActionVocabularyMatchesDatabaseShape checks every Action constant against
// the CHECK shape of the audit_logs table.
//
// The pattern is copied from ck_audit_action_shape in
// migrations/postgres/00007_audit_logs.sql. The constants come from the source
// file, so a new action is covered without editing this test.
func TestActionVocabularyMatchesDatabaseShape(t *testing.T) {
	// Keep in sync with migrations/postgres/00007_audit_logs.sql.
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

// actionConstants reads the string values of the Action constants in path.
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
