package respond

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"go/ast"
	"go/parser"
	"go/token"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

func decode(t *testing.T, body []byte) Envelope {
	t.Helper()
	var env Envelope
	if err := json.Unmarshal(body, &env); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return env
}

func TestErrorNotFound(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/x", nil)
	req.Header.Set("X-Request-Id", "trace-1")
	Error(rec, req, errs.ErrNotFound)
	if rec.Code != 404 {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
	env := decode(t, rec.Body.Bytes())
	if env.Error.Code != "not_found" || env.Error.TraceID != "trace-1" {
		t.Fatalf("body = %#v", env)
	}
}

func TestErrorUnknownHidesRawMessage(t *testing.T) {
	rec := httptest.NewRecorder()
	Error(rec, nil, errors.New("secret database detail"))
	if rec.Code != 500 {
		t.Fatalf("status = %d, want 500", rec.Code)
	}
	env := decode(t, rec.Body.Bytes())
	if env.Error.Code != "internal_error" || env.Error.Message != "internal error" {
		t.Fatalf("body = %#v", env)
	}
}

func TestErrorValidation(t *testing.T) {
	rec := httptest.NewRecorder()
	Error(rec, nil, errs.New(errs.CodeValidation, "bad input"))
	if rec.Code != 400 {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

// 错误码到状态码的映射是契约面：调用方按状态码分流，码值按业务分流。
// 这两层任何一层漂移都会让客户端走错分支，故逐码钉住。
// 422 是校验类语义（图片、定位），429 是限流与容量。
func TestStatusForEveryCode(t *testing.T) {
	want := map[errs.Code]int{
		errs.CodeValidation:        http.StatusBadRequest,
		errs.CodeUnauthorized:      http.StatusUnauthorized,
		errs.CodeForbidden:         http.StatusForbidden,
		errs.CodeNotFound:          http.StatusNotFound,
		errs.CodeConflict:          http.StatusConflict,
		errs.CodeAlreadySigned:     http.StatusConflict,
		errs.CodeSignRunning:       http.StatusConflict,
		errs.CodeWindowClosed:      http.StatusConflict,
		errs.CodeCapacityShortfall: http.StatusConflict,
		errs.CodeTaskAmbiguous:     http.StatusConflict,
		errs.CodeRateLimited:       http.StatusTooManyRequests,
		errs.CodeCapacity:          http.StatusTooManyRequests,
		errs.CodePhotoMissing:      http.StatusUnprocessableEntity,
		errs.CodePhotoInvalid:      http.StatusUnprocessableEntity,
		errs.CodePhotoRejected:     http.StatusUnprocessableEntity,
		errs.CodeLocationOutside:   http.StatusUnprocessableEntity,
		errs.CodeUpstream:          http.StatusBadGateway,
		errs.CodeInternal:          http.StatusInternalServerError,
	}
	if len(want) != 18 {
		t.Fatalf("映射表录入 %d 个码，want 18", len(want))
	}
	for code, status := range want {
		if got := statusFor(code); got != status {
			t.Errorf("statusFor(%s) = %d, want %d", code, got, status)
		}
	}

	// 新增码未登记会静默落到 default 500。逐码对账源码，双向都红。
	declared := declaredErrCodes(t)
	names := make([]string, 0, len(declared))
	for code := range declared {
		names = append(names, string(code))
	}
	sort.Strings(names)
	for _, name := range names {
		code := errs.Code(name)
		if _, ok := want[code]; !ok {
			t.Errorf("新增错误码 %s 未登记状态码映射；调用方会把它当 500", name)
		}
	}
	for code := range want {
		if _, ok := declared[code]; !ok {
			t.Errorf("映射表登记了 core/errs 中不存在的码 %s", code)
		}
	}
}

// declaredErrCodes 从 core/errs 源码收集全部稳定错误码。
// 门必须有牙：解析不到任何码即失败，**扫空不得当绿**。
func declaredErrCodes(t *testing.T) map[errs.Code]string {
	t.Helper()
	dir := filepath.Join("..", "..", "..", "core", "errs")
	pkgs, err := parser.ParseDir(token.NewFileSet(), dir, func(info os.FileInfo) bool { //nolint:staticcheck // 测试只遍历单目录；ParseDir 的 build-tags 限制不影响用例
		return !strings.HasSuffix(info.Name(), "_test.go")
	}, 0)
	if err != nil {
		t.Fatalf("parse %s: %v", dir, err)
	}
	out := map[errs.Code]string{}
	for _, pkg := range pkgs {
		for _, file := range pkg.Files {
			for _, decl := range file.Decls {
				gen, ok := decl.(*ast.GenDecl)
				if !ok || gen.Tok != token.CONST {
					continue
				}
				for _, spec := range gen.Specs {
					value, ok := spec.(*ast.ValueSpec)
					if !ok || len(value.Names) != 1 || len(value.Values) != 1 {
						continue
					}
					ident, ok := value.Type.(*ast.Ident)
					if !ok || ident.Name != "Code" {
						continue
					}
					lit, ok := value.Values[0].(*ast.BasicLit)
					if !ok || lit.Kind != token.STRING {
						continue
					}
					code, err := strconv.Unquote(lit.Value)
					if err != nil {
						t.Fatalf("unquote %s: %v", lit.Value, err)
					}
					out[errs.Code(code)] = value.Names[0].Name
				}
			}
		}
	}
	if len(out) == 0 {
		t.Fatal("core/errs 解析出 0 个错误码；门没有扫到东西")
	}
	return out
}

// 条件请求的判据：弱比较、逗号列表与 `*` 都认；无关值不命中。
func TestIfNoneMatchHit(t *testing.T) {
	const etag = `"abc"`
	cases := []struct {
		name   string
		header string
		want   bool
	}{
		{"exact", etag, true},
		{"weak", "W/" + etag, true},
		{"list", `"other", ` + etag, true},
		{"weak in list", `"other", W/` + etag, true},
		{"star", "*", true},
		{"mismatch", `"other"`, false},
		{"empty", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := IfNoneMatchHit(tc.header, etag); got != tc.want {
				t.Fatalf("IfNoneMatchHit(%q, %q) = %v, want %v", tc.header, etag, got, tc.want)
			}
		})
	}
}

// ETag 必须与写出的字节严格对应：客户端按它决定是否复用旧体。
func TestJSONWithETagWritesBodyAndMatchingValidator(t *testing.T) {
	v := map[string]string{"hello": "world"}
	rec := httptest.NewRecorder()
	JSONWithETag(rec, httptest.NewRequest(http.MethodGet, "/x", nil), http.StatusOK, "", v)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/json; charset=utf-8" {
		t.Fatalf("Content-Type = %q", ct)
	}
	if cc := rec.Header().Get("Cache-Control"); cc != "private, max-age=0" {
		t.Fatalf("Cache-Control = %q", cc)
	}
	sum := sha256.Sum256(rec.Body.Bytes())
	wantETag := `"` + hex.EncodeToString(sum[:]) + `"`
	if got := rec.Header().Get("ETag"); got != wantETag {
		t.Fatalf("ETag = %q, want %q", got, wantETag)
	}
	var decoded map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &decoded); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	if decoded["hello"] != "world" {
		t.Fatalf("body = %q", rec.Body.Bytes())
	}
}

// 304 只保留校验值：无体、无 Content-Type、无 Content-Length。
func TestJSONWithETagNotModifiedHasNoBody(t *testing.T) {
	v := map[string]string{"hello": "world"}
	rec := httptest.NewRecorder()
	JSONWithETag(rec, httptest.NewRequest(http.MethodGet, "/x", nil), http.StatusOK, "*", v)

	if rec.Code != http.StatusNotModified {
		t.Fatalf("status = %d, want 304", rec.Code)
	}
	if rec.Body.Len() != 0 {
		t.Fatalf("304 不得带体：%q", rec.Body.Bytes())
	}
	if got := rec.Header().Get("ETag"); got == "" {
		t.Fatal("304 必须带 ETag")
	}
	for _, name := range []string{"Content-Type", "Content-Length"} {
		if got := rec.Header().Get(name); got != "" {
			t.Fatalf("304 不得声明 %s：%q", name, got)
		}
	}
}

func TestJSONWithETagMismatchWritesBody(t *testing.T) {
	rec := httptest.NewRecorder()
	JSONWithETag(rec, httptest.NewRequest(http.MethodGet, "/x", nil), http.StatusOK, `"other"`,
		map[string]string{"hello": "world"})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if rec.Body.Len() == 0 {
		t.Fatal("未命中必须写响应体")
	}
}

// Marshal 失败不得留下半截成功响应。
func TestJSONWithETagMarshalErrorUsesEnvelope(t *testing.T) {
	rec := httptest.NewRecorder()
	JSONWithETag(rec, httptest.NewRequest(http.MethodGet, "/x", nil), http.StatusOK, "", make(chan int))
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", rec.Code)
	}
	env := decode(t, rec.Body.Bytes())
	if env.Error.Code != string(errs.CodeInternal) {
		t.Fatalf("code = %q, want %q", env.Error.Code, errs.CodeInternal)
	}
}

// 急停：HTTP 503，信封 code 沿用既有 capacity_exceeded（不扩枚举）。
func TestErrorPausedMapsServiceUnavailable(t *testing.T) {
	rec := httptest.NewRecorder()
	Error(rec, httptest.NewRequest(http.MethodPost, "/x", nil), errs.NewPaused())
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", rec.Code)
	}
	env := decode(t, rec.Body.Bytes())
	if env.Error.Code != string(errs.CodeCapacity) {
		t.Fatalf("code = %q, want %q（既有闭集）", env.Error.Code, errs.CodeCapacity)
	}
	if env.Error.Message != "engine is paused" {
		t.Fatalf("message = %q", env.Error.Message)
	}
}
