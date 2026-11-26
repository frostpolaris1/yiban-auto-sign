package yiban

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// P3-1：签名上传地址（uploadUri 回执的 signedUrl）的收敛判据。
//
// 生产 Provider 不得向环回/私网/链路本地/localhost/明文地址发 PUT；
// 判据用「记录 PUT 的 Transport」观测是否真的发出了请求，
// 而不是只看拒绝后的结论——守卫失效时请求必须能在测试里被看见。
//
// 本文件不打开任何测试钩子：newProductionProvider 与生产装配同形
// （Config 只有端点与 Transport）。

const (
	ssrfUploadURIPath = "/nightAttendance/student/index/uploadUri"
	ssrfSignInPath    = "/nightAttendance/student/index/signIn"
)

// uploadProbe 记录一切 PUT 尝试。
// respond=false：PUT 直接失败、不落网络，守卫失效时用例快速变红而不是等连接超时；
// respond=true：把 PUT 应答成 200，用于验证 https 公网域名仍然放行（不发网络）。
type uploadProbe struct {
	base    http.RoundTripper
	respond bool

	mu   sync.Mutex
	urls []string
}

func (p *uploadProbe) RoundTrip(req *http.Request) (*http.Response, error) {
	if req.Method != http.MethodPut {
		return p.base.RoundTrip(req)
	}
	p.mu.Lock()
	p.urls = append(p.urls, req.URL.String())
	p.mu.Unlock()
	_, _ = io.Copy(io.Discard, req.Body)
	if !p.respond {
		return nil, errors.New("unexpected attachment PUT")
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Status:     "200 OK",
		Header:     make(http.Header),
		Body:       io.NopCloser(strings.NewReader("")),
		Request:    req,
	}, nil
}

func (p *uploadProbe) count() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.urls)
}

// newProductionProvider 与生产装配同形：不打开环回上传钩子。
func newProductionProvider(t *testing.T, up *upstream, transport http.RoundTripper) *Provider {
	t.Helper()
	return NewProvider(New(Config{Endpoints: up.endpoints(), Transport: transport}))
}

// seedSignedURL 改写 uploadUri 回执里的签名地址。
func seedSignedURL(t *testing.T, up *upstream, signedURL string) {
	t.Helper()
	up.mu.Lock()
	up.uploadBody = []byte(`{"code":0,"msg":"","data":{"AttachmentFileName":"yiban_camera_1790000000.jpg","signedUrl":"` + signedURL + `"}}`)
	up.mu.Unlock()
}

// noPUTHop 确认假上游侧也没有收到 PUT（Transport 记录之外的独立观测面）。
func noPUTHop(t *testing.T, up *upstream) {
	t.Helper()
	for _, hop := range up.recordedHops() {
		if hop.Method == http.MethodPut {
			t.Fatalf("上游收到了 PUT %s", hop.Path)
		}
	}
}

// 生产 Provider：环回/私网/链路本地/localhost/明文域名一律拒绝，且零 PUT。
// 这些地址在生产里只可能来自「平台回执被篡改 / 平台被控」（台账 P3-1）。
func TestProductionRejectsNonPublicUploadTargets(t *testing.T) {
	cases := []struct {
		name   string
		signed func(up *upstream) string
	}{
		{"loopback-http", func(up *upstream) string {
			return up.server.URL + "/upload/yiban_camera_1790000000.jpg"
		}},
		{"loopback-https", func(*upstream) string { return "https://127.0.0.1/upload/x.jpg" }},
		{"loopback-ipv6-https", func(*upstream) string { return "https://[::1]/upload/x.jpg" }},
		{"localhost-https", func(*upstream) string { return "https://localhost/upload/x.jpg" }},
		{"subdomain-localhost-https", func(*upstream) string { return "https://upload.localhost/x.jpg" }},
		{"private-10-https", func(*upstream) string { return "https://10.1.2.3/upload/x.jpg" }},
		{"private-172-https", func(*upstream) string { return "https://172.16.5.4/upload/x.jpg" }},
		{"private-192-https", func(*upstream) string { return "https://192.168.1.10/upload/x.jpg" }},
		{"link-local-metadata-https", func(*upstream) string { return "https://169.254.169.254/latest/meta-data/" }},
		{"unspecified-https", func(*upstream) string { return "https://0.0.0.0/upload/x.jpg" }},
		{"multicast-https", func(*upstream) string { return "https://239.1.2.3/upload/x.jpg" }},
		{"plain-http-domain", func(*upstream) string { return "http://example.com/upload/x.jpg" }},
		{"no-host", func(*upstream) string { return "https:///upload/x.jpg" }},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			up := newUpstream(t)
			probe := &uploadProbe{base: http.DefaultTransport}
			p := newProductionProvider(t, up, probe)
			seedSignedURL(t, up, tc.signed(up))

			req, _ := uploadSignRequest("attempt-ssrf-" + tc.name)
			result, err := p.Sign(context.Background(), req)
			if err != nil {
				t.Fatalf("Sign 应把地址拒绝翻成结论而不是原生错误: %v", err)
			}
			if result.Outcome != contract.OutcomeUnknown {
				t.Fatalf("outcome = %q, want unknown", result.Outcome)
			}
			if got := probe.count(); got != 0 {
				t.Fatalf("守卫失效：发出了 %d 次 PUT: %v", got, probe.urls)
			}
			noPUTHop(t, up)
			if got := up.count(ssrfSignInPath); got != 0 {
				t.Fatalf("上传被拒后不应 signIn: %d 次", got)
			}
		})
	}
}

// 生产 Provider：https 公网域名字面量仍须放行（不实际联网，PUT 由探针应答 200）。
// 收紧守卫不能把合法签名地址一起挡掉。
func TestProductionAllowsHTTPSDomainUpload(t *testing.T) {
	up := newUpstream(t)
	probe := &uploadProbe{base: http.DefaultTransport, respond: true}
	p := newProductionProvider(t, up, probe)
	const signedURL = "https://upload.example.com/upload/yiban_camera_1790000000.jpg"
	seedSignedURL(t, up, signedURL)

	req, _ := uploadSignRequest("attempt-https-domain")
	result, err := p.Sign(context.Background(), req)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeSuccess {
		t.Fatalf("outcome = %q, want success", result.Outcome)
	}
	if got := probe.count(); got != 1 {
		t.Fatalf("https 域名应恰好 PUT 一次，实际 %d 次", got)
	}
	probe.mu.Lock()
	gotURL := probe.urls[0]
	probe.mu.Unlock()
	if gotURL != signedURL {
		t.Fatalf("PUT 地址 = %q, want %q", gotURL, signedURL)
	}
	if got := up.count(ssrfSignInPath); got != 1 {
		t.Fatalf("上传成功后应 signIn 一次，实际 %d 次", got)
	}
}
