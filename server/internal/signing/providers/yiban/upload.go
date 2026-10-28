// P5-D 附件上传：uploadUri 取签名地址 → PUT 图片字节 → 回执文件名回填 signIn。
//
// # 来源与置信度
//
// 本文件的请求顺序与字段名来自对参考程序（2117516450/yiban_signin v20260925，
// Unlicense/公有领域）发布二进制的逆向还原（UPX 脱壳 + Nuitka 常量池），
// **未经真实抓包验证**。逐条未知点以 UNVERIFIED(recon) 标注；拿到带图需求
// 账号后用探针实拍替换标注（工单 9ap）。
//
// 已由我方实拍交叉印证的部分：signIn 的 SignInfo 键序
// {Reason, AttachmentFileName, LngLat, Address} 与 7 跳实拍一致；
// 顶层三键 PhoneModel/SignInfo/OutState 一致。
package yiban

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"

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
func (c *Client) UploadAttachment(ctx context.Context, accountID string,
	cred contract.Credential, img contract.ImageRef) (string, error) {
	if len(img.Bytes) == 0 {
		return "", outcome(contract.OutcomePhotoRejected, "image is empty")
	}
	var receipt uploadReceipt
	err := c.withSessionNoRetry(ctx, accountID, cred,
		func(ctx context.Context, client *http.Client, csrf string) error {
			resp, body, err := c.requestUploadURI(ctx, client, csrf, len(img.Bytes))
			if err != nil {
				return err
			}
			if resp.StatusCode < 200 || resp.StatusCode >= 300 {
				return outcome(contract.OutcomeTransient,
					fmt.Sprintf("uploadUri status %d", resp.StatusCode))
			}
			if protocol.LooksLikeChallenge(string(body)) {
				return outcome(contract.OutcomeChallenge, "challenge page")
			}
			envelope, err := protocol.ParseAPIEnvelope(body)
			if err != nil {
				return parseOutcome(err)
			}
			if !protocol.CodeIsOK(envelope.Code) {
				return outcome(contract.OutcomePhotoRejected,
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
func (c *Client) requestUploadURI(ctx context.Context, client *http.Client,
	csrf string, size int) (*http.Response, []byte, error) {
	form := url.Values{
		"size": {strconv.Itoa(size)},
		"name": {"yiban_camera_" + strconv.FormatInt(c.clock.Now().Unix(), 10) + ".jpg"},
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
// 独立请求：不带 ubiban 会话 cookie 与 CSRF（签名地址是存储侧，不认这些）。
func (c *Client) putAttachment(ctx context.Context, signedURL string, img contract.ImageRef) error {
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

	putClient := &http.Client{Timeout: c.timeout, Transport: c.transport}
	resp, err := putClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
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
// UNVERIFIED(recon)：响应键名取自逆向常量池（AttachmentFileName / signedUrl），
// data 外层信封未实测；缺键即判未知而不是猜。
func parseUploadReceipt(data any) (uploadReceipt, error) {
	obj, ok := data.(map[string]any)
	if !ok {
		return uploadReceipt{}, outcome(contract.OutcomeUnknown, "uploadUri data is not an object")
	}
	name, nameOK := obj["AttachmentFileName"].(string)
	signed, signedOK := obj["signedUrl"].(string)
	if !nameOK || !signedOK || name == "" {
		return uploadReceipt{}, outcome(contract.OutcomeUnknown,
			"uploadUri receipt lacks AttachmentFileName or signedUrl")
	}
	return uploadReceipt{AttachmentFileName: name, SignedURL: signed}, nil
}
