package webhook_test

// adapters/webhook 的出口判据：serverchan 只看回执体 code==0，
// custom 看 2xx +「200+错误 body」判据；3xx 不被跟随；
// SSRF 负例逐字对齐旧线白名单；凭据不进错误串。

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"testing"

	webhookadapter "github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/webhook"
	corewebhook "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/webhook"
)

// roundTripFunc 是假 transport：拦截请求返回脚本化的响应。
type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// recordedSender 记录发出的请求与响应脚本。
type recordedSender struct {
	requests []*http.Request
	bodies   []string
	response roundTripFunc
}

func (r *recordedSender) client() *http.Client {
	return &http.Client{Transport: r.transport()}
}

func (r *recordedSender) transport() http.RoundTripper {
	return roundTripFunc(func(req *http.Request) (*http.Response, error) {
		body := ""
		if req.Body != nil {
			raw, _ := io.ReadAll(req.Body)
			body = string(raw)
		}
		r.requests = append(r.requests, req)
		r.bodies = append(r.bodies, body)
		return r.response(req)
	})
}

func textResponse(status int, body string) (*http.Response, error) {
	return &http.Response{
		StatusCode: status,
		Body:       io.NopCloser(strings.NewReader(body)),
		Header:     make(http.Header),
	}, nil
}

func newSender(t *testing.T, cfg webhookadapter.Config) *webhookadapter.Sender {
	t.Helper()
	sender, err := webhookadapter.New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return sender
}

// serverchan 成功：回执 JSON 对象 code==0。
func TestServerchanAcceptsCodeZero(t *testing.T) {
	rec := &recordedSender{response: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return textResponse(200, `{"code":0,"message":"ok"}`)
	})}
	sender := newSender(t, webhookadapter.Config{
		Type: "serverchan", Secret: "SCT_FAKE_TEST_KEY_000000", Client: rec.client(),
	})
	if err := sender.Send(context.Background(), corewebhook.Push{Title: "标题", Content: "正文"}); err != nil {
		t.Fatalf("Send: %v", err)
	}
	if len(rec.requests) != 1 {
		t.Fatalf("requests = %d, want 1", len(rec.requests))
	}
	req := rec.requests[0]
	if got := req.URL.Host; got != "sctapi.ftqq.com" {
		t.Fatalf("host = %q", got)
	}
	if got := req.URL.Path; !strings.HasPrefix(got, "/SCT_FAKE_TEST_KEY_000000.send") {
		t.Fatalf("path = %q, want SendKey in path", got)
	}
	// 表单：title 去换行 + desp 正文。
	if got := rec.bodies[0]; !strings.Contains(got, "desp=") {
		t.Fatalf("form = %q, want desp field", got)
	}
}

// serverchan code≠0 → refused，不可重试。
func TestServerchanRefusesNonZeroCode(t *testing.T) {
	rec := &recordedSender{response: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return textResponse(200, `{"code":40001,"message":"quota exhausted"}`)
	})}
	sender := newSender(t, webhookadapter.Config{
		Type: "serverchan", Secret: "SCT_FAKE_TEST_KEY_000000", Client: rec.client(),
	})
	err := sender.Send(context.Background(), corewebhook.Push{Title: "t", Content: "c"})
	kind, retryable := corewebhook.Classify(err)
	if kind != corewebhook.KindRefused || retryable {
		t.Fatalf("classify = (%q, %t), want refused/unretryable", kind, retryable)
	}
}

// serverchan 非 JSON / JSON 非对象 / 缺 code → bad_response 或 refused。
func TestServerchanBadAnswers(t *testing.T) {
	cases := []struct {
		name     string
		body     string
		wantKind corewebhook.Kind
		wantTry  bool
	}{
		{"non-json gateway page", "<html>Bad Gateway</html>", corewebhook.KindBadResponse, true},
		{"json array", `[{"code":0}]`, corewebhook.KindBadResponse, true},
		{"json string", `"ok"`, corewebhook.KindBadResponse, true},
		{"missing code", `{"message":"ok"}`, corewebhook.KindRefused, false},
		{"non-numeric code", `{"code":"zero"}`, corewebhook.KindRefused, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := &recordedSender{response: roundTripFunc(func(*http.Request) (*http.Response, error) {
				return textResponse(200, tc.body)
			})}
			sender := newSender(t, webhookadapter.Config{
				Type: "serverchan", Secret: "SCT_FAKE_TEST_KEY_000000", Client: rec.client(),
			})
			err := sender.Send(context.Background(), corewebhook.Push{Title: "t", Content: "c"})
			kind, retryable := corewebhook.Classify(err)
			if kind != tc.wantKind || retryable != tc.wantTry {
				t.Fatalf("classify = (%q, %t), want (%q, %t)", kind, retryable, tc.wantKind, tc.wantTry)
			}
		})
	}
}

// serverchan 的 title 去换行并按字符截 32。
func TestServerchanTitleNormalized(t *testing.T) {
	send := func(t *testing.T, title string) string {
		t.Helper()
		rec := &recordedSender{response: roundTripFunc(func(*http.Request) (*http.Response, error) {
			return textResponse(200, `{"code":0}`)
		})}
		sender := newSender(t, webhookadapter.Config{
			Type: "serverchan", Secret: "SCT_FAKE_TEST_KEY_000000", Client: rec.client(),
		})
		if err := sender.Send(context.Background(), corewebhook.Push{Title: title, Content: "x"}); err != nil {
			t.Fatalf("Send: %v", err)
		}
		return rec.bodies[0]
	}
	t.Run("line breaks become spaces", func(t *testing.T) {
		title := decodedFormTitle(t, send(t, "第一行\n第二行\r\n第三行"))
		if strings.Contains(title, "\n") || strings.Contains(title, "\r") {
			t.Fatalf("title carries line breaks: %q", title)
		}
		// 与旧线同口径：每个 CR/LF 逐字符替换成空格，不折叠连续空格。
		if want := "第一行 第二行  第三行"; title != want {
			t.Fatalf("title = %q, want %q", title, want)
		}
	})
	t.Run("truncated to 32 chars", func(t *testing.T) {
		title := decodedFormTitle(t, send(t, strings.Repeat("字", 40)))
		if len([]rune(title)) != 32 {
			t.Fatalf("title length = %d, want 32: %q", len([]rune(title)), title)
		}
	})
}

// decodedFormTitle 从 serverchan 表单体里解出 title 字段。
func decodedFormTitle(t *testing.T, form string) string {
	t.Helper()
	values, err := url.ParseQuery(form)
	if err != nil {
		t.Fatalf("parse form %q: %v", form, err)
	}
	return values.Get("title")
}

// serverchan 传输失败：超时 → timeout，其余 → connect，都可重试。
func TestServerchanTransportFailures(t *testing.T) {
	t.Run("timeout", func(t *testing.T) {
		rec := &recordedSender{response: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			return nil, context.DeadlineExceeded
		})}
		sender := newSender(t, webhookadapter.Config{
			Type: "serverchan", Secret: "SCT_FAKE_TEST_KEY_000000", Client: rec.client(),
		})
		kind, retryable := corewebhook.Classify(
			sender.Send(context.Background(), corewebhook.Push{Title: "t"}))
		if kind != corewebhook.KindTimeout || !retryable {
			t.Fatalf("classify = (%q, %t)", kind, retryable)
		}
	})
	t.Run("connect reset", func(t *testing.T) {
		rec := &recordedSender{response: roundTripFunc(func(*http.Request) (*http.Response, error) {
			return nil, errors.New("connection reset by peer")
		})}
		sender := newSender(t, webhookadapter.Config{
			Type: "serverchan", Secret: "SCT_FAKE_TEST_KEY_000000", Client: rec.client(),
		})
		kind, retryable := corewebhook.Classify(
			sender.Send(context.Background(), corewebhook.Push{Title: "t"}))
		if kind != corewebhook.KindConnect || !retryable {
			t.Fatalf("classify = (%q, %t)", kind, retryable)
		}
	})
}

// serverchan 失败路径不泄漏 SendKey：错误串只含分类。
func TestServerchanErrorHoldsNoSecret(t *testing.T) {
	const secret = "SCT_FAKE_TEST_KEY_000000"
	rec := &recordedSender{response: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return nil, errors.New("POST https://sctapi.ftqq.com/" + secret + ".send: dial refused")
	})}
	sender := newSender(t, webhookadapter.Config{
		Type: "serverchan", Secret: secret, Client: rec.client(),
	})
	err := sender.Send(context.Background(), corewebhook.Push{Title: "t", Content: "c"})
	if err == nil {
		t.Fatal("want an error")
	}
	if strings.Contains(err.Error(), secret) {
		t.Fatalf("error leaks the send key: %s", err.Error())
	}
}

// custom：200 无业务码 → sent；200+code/errcode≠0 → refused；
// 非数值回执维持 HTTP 口径。
func TestCustomReceiptJudge(t *testing.T) {
	cases := []struct {
		name     string
		body     string
		wantErr  bool
		wantKind corewebhook.Kind
	}{
		{"plain 200", `{"ok":true}`, false, ""},
		{"code zero", `{"code":0,"message":"ok"}`, false, ""},
		{"errcode zero", `{"errcode":0}`, false, ""},
		{"code non-zero", `{"code":310000,"message":"keyword"}`, true, corewebhook.KindRefused},
		{"errcode non-zero", `{"errcode":94035}`, true, corewebhook.KindRefused},
		{"code non-numeric", `{"code":"zero"}`, false, ""},
		{"non-json", "accepted", false, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			sender, rec := customSender(t, "https://public.example.test/hook", 200, tc.body)
			err := sender.Send(context.Background(), corewebhook.Push{Title: "t", Content: "c"})
			if tc.wantErr {
				kind, retryable := corewebhook.Classify(err)
				if kind != tc.wantKind || retryable {
					t.Fatalf("classify = (%q, %t), want refused/unretryable", kind, retryable)
				}
				return
			}
			if err != nil {
				t.Fatalf("Send: %v", err)
			}
			// 请求体保持旧线兼容格式 {title, content}，JSON 提交。
			if got := rec.bodies[0]; !strings.Contains(got, `"title"`) || !strings.Contains(got, `"content"`) {
				t.Fatalf("payload = %q", got)
			}
			if got := rec.requests[0].Header.Get("Content-Type"); got != "application/json" {
				t.Fatalf("content-type = %q", got)
			}
		})
	}
}

// custom：4xx → refused 不重试；5xx → connect 可重试；3xx 不被跟随。
func TestCustomStatusJudge(t *testing.T) {
	t.Run("4xx refused", func(t *testing.T) {
		sender, _ := customSender(t, "https://public.example.test/hook", 403, "forbidden")
		kind, retryable := corewebhook.Classify(
			sender.Send(context.Background(), corewebhook.Push{Title: "t"}))
		if kind != corewebhook.KindRefused || retryable {
			t.Fatalf("classify = (%q, %t)", kind, retryable)
		}
	})
	t.Run("5xx retryable", func(t *testing.T) {
		sender, _ := customSender(t, "https://public.example.test/hook", 503, "busy")
		kind, retryable := corewebhook.Classify(
			sender.Send(context.Background(), corewebhook.Push{Title: "t"}))
		if kind != corewebhook.KindConnect || !retryable {
			t.Fatalf("classify = (%q, %t)", kind, retryable)
		}
	})
	t.Run("3xx not followed", func(t *testing.T) {
		sender, rec := customSender(t, "https://public.example.test/hook", 302, "")
		rec.response = roundTripFunc(func(req *http.Request) (*http.Response, error) {
			resp, _ := textResponse(302, "")
			resp.Header.Set("Location", "https://public.example.test/redirected")
			return resp, nil
		})
		err := sender.Send(context.Background(), corewebhook.Push{Title: "t"})
		kind, _ := corewebhook.Classify(err)
		if kind != corewebhook.KindRefused {
			t.Fatalf("classify = %q, want refused", kind)
		}
		if len(rec.requests) != 1 {
			t.Fatalf("requests = %d, want exactly 1 (no redirect follow)", len(rec.requests))
		}
	})
}

// custom 的 SSRF 拒发：白名单外地址在发请求前拒（unsafe_url，不可重试），
// 端口根本没有被调用。
func TestCustomRejectsUnsafeURLBeforeDial(t *testing.T) {
	cases := []string{
		"http://public.example.test/hook",        // 非 https
		"https://2130706433/hook",                // 十进制整数
		"https://0x7f000001/hook",                // 0x 十六进制
		"https://0177.0.0.1/hook",                // 前导零八进制
		"https://127.1/hook",                     // 短式回环
		"https://localhost/hook",                 // localhost
		"https://localhost./hook",                // 尾点归一后同名
		"https://127.0.0.1:443\\@evil.test/hook", // 反斜杠
		"https://evil.test@127.0.0.1/hook",       // userinfo 掩护下的回环
		"https://169.254.169.254/latest",         // 链路本地（云元数据）
		"https://100.100.100.200/meta",           // CGNAT 段（阿里云元数据）
		"https://100.64.0.1/hook",                // CGNAT 段内地址
		"https://10.0.0.1/hook",                  // 私网
		"https://192.168.1.1/hook",               // 私网
		"https://172.16.0.1/hook",                // 私网
		"https://[::1]/hook",                     // v6 回环
		"https://[::ffff:127.0.0.1]/hook",        // v4-mapped 回环
		"https://240.0.0.1/hook",                 // 保留段
	}
	for _, raw := range cases {
		t.Run(raw, func(t *testing.T) {
			rec := &recordedSender{}
			sender := newSender(t, webhookadapter.Config{
				Type: "custom", Secret: raw, Client: rec.client(),
			})
			err := sender.Send(context.Background(), corewebhook.Push{Title: "t"})
			kind, retryable := corewebhook.Classify(err)
			if kind != corewebhook.KindUnsafeURL || retryable {
				t.Fatalf("classify = (%q, %t), want unsafe_url/unretryable", kind, retryable)
			}
			if len(rec.requests) != 0 {
				t.Fatalf("unsafe URL reached the wire: %d requests", len(rec.requests))
			}
		})
	}
}

// custom 的解析复核：解析到内网/保留段的域名拒发；解析失败拒（fail-closed）；
// 解析到公网放行。
func TestCustomResolvedReview(t *testing.T) {
	publicHost := "https://push.example.test/hook"
	private := net.ParseIP("10.1.2.3")
	public := net.ParseIP("203.0.113.7")

	t.Run("domain resolving into private range", func(t *testing.T) {
		rec := &recordedSender{}
		sender := newSender(t, webhookadapter.Config{
			Type: "custom", Secret: publicHost, Client: rec.client(),
			Lookup: func(context.Context, string) ([]net.IPAddr, error) {
				return []net.IPAddr{{IP: private}}, nil
			},
		})
		err := sender.Send(context.Background(), corewebhook.Push{Title: "t"})
		if kind, _ := corewebhook.Classify(err); kind != corewebhook.KindUnsafeURL {
			t.Fatalf("classify = %q, want unsafe_url", kind)
		}
		if len(rec.requests) != 0 {
			t.Fatal("private-resolving domain reached the wire")
		}
	})
	t.Run("resolution failure fails closed", func(t *testing.T) {
		rec := &recordedSender{}
		sender := newSender(t, webhookadapter.Config{
			Type: "custom", Secret: publicHost, Client: rec.client(),
			Lookup: func(context.Context, string) ([]net.IPAddr, error) {
				return nil, errors.New("no such host")
			},
		})
		err := sender.Send(context.Background(), corewebhook.Push{Title: "t"})
		if kind, _ := corewebhook.Classify(err); kind != corewebhook.KindUnsafeURL {
			t.Fatalf("classify = %q, want unsafe_url", kind)
		}
	})
	t.Run("public resolution passes", func(t *testing.T) {
		rec := &recordedSender{response: roundTripFunc(func(*http.Request) (*http.Response, error) {
			return textResponse(200, `{"ok":true}`)
		})}
		sender := newSender(t, webhookadapter.Config{
			Type: "custom", Secret: publicHost, Client: rec.client(),
			Lookup: func(context.Context, string) ([]net.IPAddr, error) {
				return []net.IPAddr{{IP: public}}, nil
			},
		})
		if err := sender.Send(context.Background(), corewebhook.Push{Title: "t", Content: "c"}); err != nil {
			t.Fatalf("Send: %v", err)
		}
	})
}

// custom 直连复核 IP：请求 URL 的 host 保持原样（TLS SNI 与 Host 头
// 不漂移）；拨号地址改写成复核 IP 由白盒测试钉（dialAddrs）。
func TestCustomRequestKeepsOriginalHost(t *testing.T) {
	rec := &recordedSender{response: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return textResponse(200, `{}`)
	})}
	sender := newSender(t, webhookadapter.Config{
		Type: "custom", Secret: "https://push.example.test/hook", Client: rec.client(),
		Lookup: func(context.Context, string) ([]net.IPAddr, error) {
			return []net.IPAddr{{IP: net.ParseIP("203.0.113.7")}}, nil
		},
	})
	if err := sender.Send(context.Background(), corewebhook.Push{Title: "t", Content: "c"}); err != nil {
		t.Fatalf("Send: %v", err)
	}
	if got := rec.requests[0].URL.Hostname(); got != "push.example.test" {
		t.Fatalf("request host = %q, want the original host (SNI stays)", got)
	}
}

// custom 的超时归 timeout。
func TestCustomTimeout(t *testing.T) {
	sender, _ := customSenderWithResponse(t, "https://public.example.test/hook",
		func(*http.Request) (*http.Response, error) {
			return nil, context.DeadlineExceeded
		})
	kind, retryable := corewebhook.Classify(
		sender.Send(context.Background(), corewebhook.Push{Title: "t"}))
	if kind != corewebhook.KindTimeout || !retryable {
		t.Fatalf("classify = (%q, %t)", kind, retryable)
	}
}

// New 的词表守卫：未知类型与空密钥都拒绝构造。
func TestNewGuards(t *testing.T) {
	if _, err := webhookadapter.New(webhookadapter.Config{Type: "serverchan", Secret: " "}); err == nil {
		t.Fatal("empty secret must fail")
	}
	if _, err := webhookadapter.New(webhookadapter.Config{
		Type: "pigeon", Secret: "SCT_FAKE_TEST_KEY_000000"}); err == nil {
		t.Fatal("unknown outlet type must fail")
	}
}

// MaskedHost 只留 scheme+host，去 path/query/userinfo。
func TestMaskedHost(t *testing.T) {
	cases := map[string]string{
		"https://sctapi.ftqq.com/SCT_FAKE.send":     "https://sctapi.ftqq.com",
		"https://user:pass@hook.example.test/a?b=1": "https://hook.example.test",
		"http://127.0.0.1:9000/x":                   "http://127.0.0.1:9000",
	}
	for raw, want := range cases {
		got := webhookadapter.MaskedHost(raw)
		if got != want {
			t.Fatalf("MaskedHost(%q) = %q, want %q", raw, got, want)
		}
	}
}

// customSender 构造 custom 出口与注入面（公网解析 + 固定响应）。
func customSender(t *testing.T, rawURL string, status int, body string) (*webhookadapter.Sender, *recordedSender) {
	t.Helper()
	return customSenderWithResponse(t, rawURL, func(*http.Request) (*http.Response, error) {
		return textResponse(status, body)
	})
}

func customSenderWithResponse(t *testing.T, rawURL string, respond roundTripFunc) (*webhookadapter.Sender, *recordedSender) {
	t.Helper()
	rec := &recordedSender{response: respond}
	sender := newSender(t, webhookadapter.Config{
		Type: "custom", Secret: rawURL, Client: rec.client(),
		Lookup: func(context.Context, string) ([]net.IPAddr, error) {
			return []net.IPAddr{{IP: net.ParseIP("203.0.113.7")}}, nil
		},
	})
	return sender, rec
}
