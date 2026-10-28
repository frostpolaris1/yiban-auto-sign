package yiban

import (
	"bytes"
	"context"
	"strconv"
	"strings"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// P5-D 上传链判据。
//
// 夹具与请求形状来源：对参考程序二进制的逆向还原（**未经实拍**）。
// 本文件的断言钉的是「我方实现按逆向形状发出请求」，不构成平台证据；
// 实拍替换点在 upload.go 内以 UNVERIFIED(recon) 标注（工单 9ap）。

func uploadSignRequest(attempt string) (contract.SignRequest, contract.ImageRef) {
	req := providerSignRequest(attempt)
	img := contract.ImageRef{
		ID:     "photo-1",
		Mime:   "image/jpeg",
		Width:  12,
		Height: 8,
		SHA256: strings.Repeat("a", 64),
		Bytes:  []byte("jpeg-bytes-for-upload"),
	}
	req.Image = &img
	req.Location = contract.Location{Mode: contract.LocationFixed, Lng: 118.1, Lat: 32.0, Reason: "示例范围外原因"}
	return req, img
}

// 带图签到：顺序 uploadUri → PUT → signIn；回执文件名回填两处既有字段。
func TestSignWithImageUploadsFirst(t *testing.T) {
	p, up := newTestProvider(t)
	req, img := uploadSignRequest("attempt-upload-1")

	result, err := p.Sign(context.Background(), req)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeSuccess {
		t.Fatalf("outcome = %q, want success", result.Outcome)
	}

	// 请求顺序：uploadUri 在 PUT 之前、PUT 在 signIn 之前。
	var order []string
	for _, hop := range up.recordedHops() {
		switch {
		case hop.Path == "/nightAttendance/student/index/uploadUri":
			order = append(order, "uploadUri")
		case strings.HasPrefix(hop.Path, "/upload/"):
			order = append(order, "put")
		case hop.Path == "/nightAttendance/student/index/signIn":
			order = append(order, "signIn")
		}
	}
	got := strings.Join(order, ",")
	want := "uploadUri,put,signIn"
	if got != want {
		t.Fatalf("请求顺序 = %q, want %q", got, want)
	}

	// uploadUri：CSRF 在 query；size/name 在 form（UNVERIFIED(recon) 形状）。
	up.mu.Lock()
	uploadForm, uploadQuery := up.uploadForm, up.uploadQuery
	putHeaders, putBody, putPath := up.putHeaders, up.putBody, up.putPath
	signForm := up.signForm
	up.mu.Unlock()
	if uploadQuery.Get("CSRF") == "" {
		t.Fatalf("uploadUri 缺 CSRF query")
	}
	if got := uploadForm.Get("size"); got != strconv.Itoa(len(img.Bytes)) {
		t.Fatalf("uploadUri size = %q, want %d", got, len(img.Bytes))
	}
	name := uploadForm.Get("name")
	if !strings.HasPrefix(name, "yiban_camera_") || !strings.HasSuffix(name, ".jpg") {
		t.Fatalf("uploadUri name = %q, want yiban_camera_<ts>.jpg", name)
	}

	// PUT：方法必须 PUT、专用 UA、image/jpeg、字节一致，且**不带会话 cookie/CSRF**。
	up.mu.Lock()
	putMethod := up.putMethod
	up.mu.Unlock()
	if putMethod != "PUT" {
		t.Fatalf("上传方法 = %q, want PUT", putMethod)
	}
	if putPath != "/upload/yiban_camera_1790000000.jpg" {
		t.Fatalf("PUT path = %q", putPath)
	}
	if got := putHeaders.Get("User-Agent"); got != photoPutUA {
		t.Fatalf("PUT UA = %q, want UA_PHOTO_PUT", got)
	}
	if got := putHeaders.Get("Content-Type"); got != "image/jpeg" {
		t.Fatalf("PUT Content-Type = %q", got)
	}
	if got := putHeaders.Get("Cookie"); got != "" {
		t.Fatalf("PUT 不应携带会话 cookie：%q", got)
	}
	if !bytes.Equal(putBody, img.Bytes) {
		t.Fatalf("PUT body 与图片字节不一致：len=%d vs %d", len(putBody), len(img.Bytes))
	}

	// signIn：顶层与 SignInfo 的 AttachmentFileName 都是回执名；Reason 已随行。
	if got := signForm.Get("AttachmentFileName"); got != "yiban_camera_1790000000.jpg" {
		t.Fatalf("signIn 顶层 AttachmentFileName = %q", got)
	}
	signInfoJSON := signForm.Get("SignInfo")
	if !strings.Contains(signInfoJSON, `"AttachmentFileName": "yiban_camera_1790000000.jpg"`) {
		t.Fatalf("SignInfo 未回填回执名：%s", signInfoJSON)
	}
	if !strings.Contains(signInfoJSON, `"Reason": "示例范围外原因"`) {
		t.Fatalf("SignInfo 未携带 Reason：%s", signInfoJSON)
	}
}

// 无图签到：不发起上传，AttachmentFileName 送空串（与实拍无图提交一致）。
func TestSignWithoutImageSkipsUpload(t *testing.T) {
	p, up := newTestProvider(t)
	req := providerSignRequest("attempt-no-image")

	result, err := p.Sign(context.Background(), req)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeSuccess {
		t.Fatalf("outcome = %q", result.Outcome)
	}
	if n := up.count("/nightAttendance/student/index/uploadUri"); n != 0 {
		t.Fatalf("无图时不应请求 uploadUri：%d 次", n)
	}
	up.mu.Lock()
	form := up.signForm
	up.mu.Unlock()
	if got := form.Get("AttachmentFileName"); got != "" {
		t.Fatalf("无图时 AttachmentFileName = %q, want 空串", got)
	}
}

// uploadUri 业务码非 0：码表未实拍，判 unknown（可重试）而不是 photo_rejected
// （后者当日落 failed 不再重试，评审 M2）。不发 PUT、不提交 signIn。
func TestSignUploadNonZeroCodeIsUnknown(t *testing.T) {
	p, up := newTestProvider(t)
	up.mu.Lock()
	up.uploadBody = []byte(`{"code":4001,"msg":"unrecognised","data":null}`)
	up.mu.Unlock()

	req, _ := uploadSignRequest("attempt-upload-rejected")
	result, err := p.Sign(context.Background(), req)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeUnknown {
		t.Fatalf("outcome = %q, want unknown", result.Outcome)
	}
	if n := up.count("/upload/yiban_camera_1790000000.jpg"); n != 0 {
		t.Fatalf("uploadUri 失败后不应 PUT：%d 次", n)
	}
	if n := up.count("/nightAttendance/student/index/signIn"); n != 0 {
		t.Fatalf("上传失败后不应 signIn：%d 次", n)
	}
}

// 签名地址非 https（且非环回）：fail-closed 判 unknown，不发 PUT。
func TestSignUploadNonHTTPSignedURLRejected(t *testing.T) {
	p, up := newTestProvider(t)
	up.mu.Lock()
	up.uploadBody = []byte(`{"code":0,"msg":"","data":{"AttachmentFileName":"yiban_camera_1790000000.jpg","signedUrl":"http://example.com/upload/x.jpg"}}`)
	up.mu.Unlock()

	req, _ := uploadSignRequest("attempt-upload-http-url")
	result, err := p.Sign(context.Background(), req)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeUnknown {
		t.Fatalf("outcome = %q, want unknown", result.Outcome)
	}
	if n := up.count("/nightAttendance/student/index/signIn"); n != 0 {
		t.Fatalf("方案异常后不应 signIn：%d 次", n)
	}
}

// PUT 失败：按 transient 处理，不提交 signIn。
func TestSignPutFailureIsTransient(t *testing.T) {
	p, up := newTestProvider(t)
	up.mu.Lock()
	up.putStatus = 500
	up.mu.Unlock()

	req, _ := uploadSignRequest("attempt-put-500")
	result, err := p.Sign(context.Background(), req)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeTransient {
		t.Fatalf("outcome = %q, want transient", result.Outcome)
	}
	if n := up.count("/nightAttendance/student/index/signIn"); n != 0 {
		t.Fatalf("PUT 失败后不应 signIn：%d 次", n)
	}
}

// 回执缺键：判未知，不猜默认值，不提交 signIn。
func TestSignUploadReceiptMissingKeysIsUnknown(t *testing.T) {
	p, up := newTestProvider(t)
	up.mu.Lock()
	up.uploadBody = []byte(`{"code":0,"msg":"","data":{}}`)
	up.mu.Unlock()

	req, _ := uploadSignRequest("attempt-upload-nokeys")
	result, err := p.Sign(context.Background(), req)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeUnknown {
		t.Fatalf("outcome = %q, want unknown", result.Outcome)
	}
	if n := up.count("/nightAttendance/student/index/signIn"); n != 0 {
		t.Fatalf("回执异常后不应 signIn：%d 次", n)
	}
}

// 回执 signedUrl 为空串：与缺键同档判未知（fail-closed，不把空地址丢给 PUT）。
func TestSignUploadReceiptEmptySignedURLIsUnknown(t *testing.T) {
	p, up := newTestProvider(t)
	up.mu.Lock()
	up.uploadBody = []byte(`{"code":0,"msg":"","data":{"AttachmentFileName":"yiban_camera_1790000000.jpg","signedUrl":""}}`)
	up.mu.Unlock()

	req, _ := uploadSignRequest("attempt-upload-emptyurl")
	result, err := p.Sign(context.Background(), req)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeUnknown {
		t.Fatalf("outcome = %q, want unknown", result.Outcome)
	}
	if n := up.count("/nightAttendance/student/index/signIn"); n != 0 {
		t.Fatalf("回执异常后不应 signIn：%d 次", n)
	}
}
