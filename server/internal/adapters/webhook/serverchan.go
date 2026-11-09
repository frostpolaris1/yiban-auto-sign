// Server酱 Turbo 出口：POST https://sctapi.ftqq.com/{SendKey}.send，
// 表单字段 title + desp。成功判据是响应为 JSON 对象且 code==0；
// 非 JSON / JSON 非对象按 bad_response（可重试），code≠0 按 refused。
// HTTP 状态码不参与成功判定（与旧线一致：判据只看回执体）。
package webhook

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"

	corewebhook "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/webhook"
)

// serverchanURL 拼出口地址。SendKey 拼在 URL path 里：
// 失败时这个字符串只活在错误链内部，Error() 不输出它。
func serverchanURL(secret string) string {
	return "https://" + serverchanHost + "/" + secret + ".send"
}

// sendServerchan 发一条 Server酱推送。
// title 过 normalizeTitle（去换行、截 32），desp 为纯文本正文。
func (s *Sender) sendServerchan(ctx context.Context, push corewebhook.Push) error {
	ctx, cancel := sendCtx(ctx)
	defer cancel()
	form := url.Values{}
	form.Set("title", normalizeTitle(push.Title))
	form.Set("desp", push.Content)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		serverchanURL(s.secret), strings.NewReader(form.Encode()))
	if err != nil {
		return &corewebhook.DeliveryError{
			Kind: corewebhook.KindRefused,
			Err:  errors.New("webhook: cannot build serverchan request"),
		}
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := noRedirect(s.client).Do(req)
	if err != nil {
		return classifyTransport(err)
	}
	defer drainClose(resp.Body)
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBody))
	if err != nil {
		// 回执读到一半断线：没有可读的送达回执，按可重试的坏响应记账。
		return &corewebhook.DeliveryError{
			Kind: corewebhook.KindBadResponse, Retryable: true, Err: err,
		}
	}
	// 非 JSON（网关错误页与半截响应常见）→ 可重试的坏响应。
	var payload any
	if err := json.Unmarshal(body, &payload); err != nil {
		return &corewebhook.DeliveryError{
			Kind: corewebhook.KindBadResponse, Retryable: true,
			Err: errors.New("webhook: serverchan answer is not JSON"),
		}
	}
	// 合法 JSON 但非对象（列表/字符串/数字）：没有可读的送达回执字段，
	// 按坏响应走可重试，不定罪也不放行。
	object, ok := payload.(map[string]any)
	if !ok {
		return &corewebhook.DeliveryError{
			Kind: corewebhook.KindBadResponse, Retryable: true,
			Err: errors.New("webhook: serverchan answer is not a JSON object"),
		}
	}
	// code 缺失或非数值都无法证明送达：按拒收处理（不可重试）。
	code, ok := object["code"].(float64)
	if !ok {
		return &corewebhook.DeliveryError{
			Kind: corewebhook.KindRefused,
			Err:  errors.New("webhook: serverchan answer carries no numeric code"),
		}
	}
	if code != 0 {
		// 配额耗尽 / 限频 / 密钥错误等：拒收是确定性结果，不可重试。
		return &corewebhook.DeliveryError{
			Kind: corewebhook.KindRefused,
			Err:  errors.New("webhook: serverchan refused the push"),
		}
	}
	return nil
}
