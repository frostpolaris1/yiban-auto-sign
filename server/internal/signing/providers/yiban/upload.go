// P5-D 附件上传：uploadUri 取签名地址 → PUT 图片字节 → 回执文件名回填 signIn。
//
// # 来源与置信度
//
// 本文件的请求顺序与字段名来自对参考程序（2117516450/yiban_signin v20260925，
// Unlicense/公有领域）发布二进制的逆向还原（UPX 脱壳 + Nuitka 常量池），
// **未经真实抓包验证**。逐条未知点以 UNVERIFIED(recon) 标注；拿到带图需求
// 账号后用探针实拍替换标注。
//
// 已由我方实拍交叉印证的部分：signIn 的 SignInfo 键序
// {Reason, AttachmentFileName, LngLat, Address} 与 7 跳实拍一致；
// 顶层三键 PhoneModel/SignInfo/OutState 一致。
//
// # 签名地址的收敛（台账 P3-1）
//
// putAttachment 只向 https 公网字面量地址发 PUT：scheme 必须是 https，
// 主机不得是 localhost/*.localhost，不得是环回/私网/链路本地/未指定/组播
// 的 IP 字面量；带 zone-id 的 IPv6（[::1%25lo]）一律拒。域名不做 DNS
// 解析（残余风险见 allowedUploadURL 的注释）；非规范 IPv4 写法（127.1、
// 2130706433、0x7f000001、0177.0.0.1）同样按域名放行，靠解析失败兜底。
//
// 环回明文仅测试：Config.allowLoopbackUpload（未导出）→ Client.allowLoopback，
// 只额外放行 http://127.0.0.1 与 http://[::1] 的 httptest 假上游；
// 生产构造（yiban.New(yiban.Config{...})）无法打开，也不读环境变量/配置文件。
package yiban

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/protocol"
)

// photoPutUA 是上传图片专用 UA（逆向常量 UA_PHOTO_PUT 原文）。
// UNVERIFIED(recon)：PUT 是否校验 UA 未实测；照抄参考实现以贴合其可见行为。
const photoPutUA = "Mozilla/5.0 (Linux; Android 12; Redmi K30 Pro Build/SKQ1.211006.001; wv) " +
	"AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/120.0.6099.43 " +
	"Mobile Safari/537.36;webank/h5face;webank/2.0 yiban_android/5.1.12 Edg/129.0.0.0"

// uploadReceipt 是 uploadUri 的回执。
type uploadReceipt struct {
	AttachmentFileName string
	SignedURL          string
}

// UploadAttachment 完成一次附件上传，返回回填 signIn 用的 AttachmentFileName。
//
// 非幂等：uploadUri 会登记一次附件，999 不自动重试（与 signIn 同档）。
//
// UNVERIFIED(recon)：错误分档是按我方档位语义的推断映射，平台真实错误码未实拍：
// HTTP 非 2xx/网络→transient；回执缺键→unknown；**uploadUri 业务码非 0→unknown
// （可重试）**——码表未知，不把未识别码硬判 photo_rejected（那会当日落 failed、
// 不再重试，评审 M2）。实拍后用真实码表细化。
//
// UNVERIFIED(recon)：uploadUri 请求体用 form（size/name）；逆向报告只确认上传体
// 是 image/jpeg（那是 PUT 的体），请求侧编码未实测。
//
// 激活前提：宿主在 Target.RequiresPhoto 时才会带图进来；该标志的语义
// （IsNeedPhoto 取值表）未实拍，当前恒 false ⇒ 本链只在测试与未来接线时执行（评审 L3）。
func (c *Client) UploadAttachment(ctx context.Context, accountID string,
	cred contract.Credential, img contract.ImageRef) (string, error) {
	if len(img.Bytes) == 0 {
		return "", outcome(contract.OutcomePhotoRejected, "image is empty")
	}
	var receipt uploadReceipt
	err := c.withSessionNoRetry(ctx, accountID, cred,
		func(ctx context.Context, client *http.Client, csrf string) error {
			resp, body, err := c.requestUploadURI(ctx, client, csrf, img)
			if err != nil {
				return err
			}
			if wafBlocked(string(body)) {
				return blockedOutcome(string(body))
			}
			if resp.StatusCode < 200 || resp.StatusCode >= 300 {
				return outcome(contract.OutcomeTransient,
					fmt.Sprintf("uploadUri status %d", resp.StatusCode))
			}
			envelope, err := protocol.ParseAPIEnvelope(body)
			if err != nil {
				return parseOutcome(err)
			}
			if !protocol.CodeIsOK(envelope.Code) {
				return outcome(contract.OutcomeUnknown,
					fmt.Sprintf("uploadUri code %v", envelope.Code))
			}
			parsed, err := parseUploadReceipt(envelope.Data)
			if err != nil {
				return err
			}
			receipt = parsed
			if err := c.putAttachment(ctx, receipt.SignedURL, img); err != nil {
				return err
			}
			return nil
		})
	if err != nil {
		return "", err
	}
	return receipt.AttachmentFileName, nil
}

// requestUploadURI 申请签名上传地址。
//
// UNVERIFIED(recon)：size/name 放 form body 是逆向推断的最可能形状；
// 参考二进制的参数放置（body vs query）无法从常量池确定。
// 名称扩展名跟随图片来源的 MIME（参考实现先转 JPEG 再传，恒 .jpg；
// 我方图库可能存 PNG，字节与扩展名必须一致——评审 M4）。
func (c *Client) requestUploadURI(ctx context.Context, client *http.Client,
	csrf string, img contract.ImageRef) (*http.Response, []byte, error) {
	form := url.Values{
		"size": {strconv.Itoa(len(img.Bytes))},
		"name": {"yiban_camera_" + strconv.FormatInt(c.clock.Now().Unix(), 10) + extForMime(img.Mime)},
	}
	req, err := newRequest(ctx, http.MethodPost,
		withQuery(c.endpoints.UploadUri, url.Values{"CSRF": {csrf}}),
		bytes.NewBufferString(form.Encode()))
	if err != nil {
		return nil, nil, err
	}
	setAppHeaders(req)
	addCSRF(req, csrf)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
	return c.do(client, req)
}

// putAttachment 把图片字节 PUT 到签名地址。
// 独立请求：不带 uyiban 会话 cookie 与 CSRF（签名地址是存储侧，不认这些）。
// **不跟随重定向**（评审 M3）：301/302/303 会把方法降为 GET 丢掉请求体，
// 末跳 2xx 会被误判成功；其余链同样显式 noRedirect（本函数的
// CheckRedirect 保持 ErrUseLastResponse，不因地址收敛而放宽）。
//
// 地址先过 allowedUploadURL（P3-1）：生产只接受 https 公网字面量；
// 环回明文只在测试钩子 Client.allowLoopback 打开时放行。
func (c *Client) putAttachment(ctx context.Context, signedURL string, img contract.ImageRef) error {
	if ok, reason := allowedUploadURL(signedURL, c.allowLoopback); !ok {
		return outcome(contract.OutcomeUnknown, "uploadUri signedUrl rejected: "+reason)
	}
	mime := img.Mime
	if mime == "" {
		mime = "image/jpeg"
	}
	req, err := newRequest(ctx, http.MethodPut, signedURL, bytes.NewReader(img.Bytes))
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", photoPutUA)
	req.Header.Set("Content-Type", mime)
	req.ContentLength = int64(len(img.Bytes))

	putClient := &http.Client{
		Timeout:   c.timeout,
		Transport: c.transport,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	resp, err := putClient.Do(req)
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()
	// 丢弃回读体但限长，防止异常上游灌爆内存。
	if _, err := io.Copy(io.Discard, io.LimitReader(resp.Body, maxBodyBytes)); err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return outcome(contract.OutcomeTransient,
			fmt.Sprintf("attachment PUT status %d", resp.StatusCode))
	}
	return nil
}

// parseUploadReceipt 从 uploadUri 的 data 取回执。
//
// UNVERIFIED(recon)：响应键名取自逆向常量池（AttachmentFileName / signedUrl，
// 另有变量名 attachment；此处对文件名做该键的兜底读取）。data 外层信封未实测；
// 缺键即判未知而不是猜。
func parseUploadReceipt(data any) (uploadReceipt, error) {
	obj, ok := data.(map[string]any)
	if !ok {
		return uploadReceipt{}, outcome(contract.OutcomeUnknown, "uploadUri data is not an object")
	}
	name, nameOK := obj["AttachmentFileName"].(string)
	if !nameOK || name == "" {
		// 逆向常量池另有 attachment 键，作为回执文件名兜底（recon 未定键序）。
		name, nameOK = obj["attachment"].(string)
	}
	signed, signedOK := obj["signedUrl"].(string)
	if !nameOK || !signedOK || name == "" || signed == "" {
		return uploadReceipt{}, outcome(contract.OutcomeUnknown,
			"uploadUri receipt lacks AttachmentFileName or signedUrl")
	}
	return uploadReceipt{AttachmentFileName: name, SignedURL: signed}, nil
}

// extForMime 按图片 MIME 给上传文件名选扩展名，保证名字与字节一致
// （参考实现先转 JPEG 再传，恒 .jpg；我方图库可能存 PNG）。
func extForMime(mime string) string {
	switch mime {
	case "image/png":
		return ".png"
	default:
		return ".jpg"
	}
}

// allowedUploadURL 判定签名上传地址能否发 PUT（台账 P3-1）。
//
// 生产（allowLoopback=false）只接受 https + 公网字面量主机：
//   - scheme 必须是 https（明文一律拒绝）；
//   - 主机不得是 localhost 或 *.localhost；
//   - IP 字面量不得是环回/私网/链路本地（单播与组播）/未指定/组播地址；
//   - 解析失败或没有主机名一律拒绝。
//
// 测试钩子（allowLoopback=true，仅 Config.allowLoopbackUpload 经 New 复制）
// 额外放行 http 且主机恰为 127.0.0.1 或 ::1 的 httptest 假上游。
// 钩子只放宽「明文 + 这两个环回字面量」，https 环回、私网、localhost
// 即使打开钩子也仍然拒绝。
//
// # 残余风险（本层不解决）
//
// 域名主机在此只做字面量检查，不做 DNS 解析或公网白名单：签名地址来自
// 平台回执（数据），解析结果在检查与拨号之间可变——这里解析一次再放行，
// 拨号时可能已被重新指向内网（TOCTOU），反而给出「已校验」的错觉。
// 真正的约束应在出口网络策略（默认拒绝内网段/元数据地址）与平台回执
// 可信度上；本函数只保证方案与字面量的 fail-closed 下限。此外 CGNAT
// （100.64.0.0/10）等特殊用途段不在六类拒绝之内，属已知残余边界。
// 非规范 IPv4 写法（127.1、2130706433、0x7f000001、0177.0.0.1）不被
// net.ParseIP 识别而按域名放行，目前靠解析失败兜底；若要收紧，应对齐
// adapters/webhook/ssrf.go 的 isIPv4LiteralLike 另加字面量形状识别，
// 本批不复制那套逻辑。
func allowedUploadURL(raw string, allowLoopback bool) (bool, string) {
	parsed, err := url.Parse(raw)
	if err != nil {
		return false, "unparsable URL"
	}
	if parsed.Host == "" {
		return false, "missing host"
	}
	// Hostname() 去端口与 IPv6 方括号；末尾点归一（127.0.0.1. / localhost.），
	// 用 TrimRight 吃掉全部末尾点，避免 127.0.0.1.. 只归一一个后漏到域名分支。
	host := strings.ToLower(strings.TrimRight(parsed.Hostname(), "."))
	if host == "" {
		return false, "missing host"
	}
	// IPv6 zone-id（RFC 6874，如 https://[::1%25lo]/ → Hostname()=="::1%lo"）
	// 会让 net.ParseIP 失明，从而绕开六类 IP 判定；上传地址不需要 zone，一律拒。
	if strings.Contains(host, "%") {
		return false, "IPv6 zone identifier is not allowed"
	}
	if parsed.Scheme == "http" && allowLoopback && isTestLoopbackHost(host) {
		return true, ""
	}
	if parsed.Scheme != "https" {
		if parsed.Scheme == "" {
			return false, "missing scheme"
		}
		return false, "scheme " + parsed.Scheme + " is not https"
	}
	if host == "localhost" || strings.HasSuffix(host, ".localhost") {
		return false, "localhost is not a public host"
	}
	if ip := net.ParseIP(host); ip != nil {
		if reason, blocked := blockedUploadIPReason(ip); blocked {
			return false, reason
		}
	}
	return true, ""
}

// blockedUploadIPReason 给出 IP 字面量被拒的理由；六类都是本机或内网可达面。
// 并列命中时按环回→私网→链路本地→链路本地组播→未指定→组播的顺序报。
func blockedUploadIPReason(ip net.IP) (string, bool) {
	switch {
	case ip.IsLoopback():
		return "loopback address", true
	case ip.IsPrivate():
		return "private address", true
	case ip.IsLinkLocalUnicast():
		return "link-local address", true
	case ip.IsLinkLocalMulticast():
		return "link-local multicast address", true
	case ip.IsUnspecified():
		return "unspecified address", true
	case ip.IsMulticast():
		return "multicast address", true
	}
	return "", false
}

// isTestLoopbackHost 仅供测试钩子：只认 httptest 会绑定的两个环回字面量。
// localhost 不在此列——测试上游用 httptest.NewServer 的 127.0.0.1 形式。
func isTestLoopbackHost(host string) bool {
	return host == "127.0.0.1" || host == "::1"
}
