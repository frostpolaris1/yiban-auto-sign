// Command probe-login runs the new-stack yiban login chain against the real
// upstream exactly once and captures every wire request/response. One-shot
// diagnostic (4sk): credentials are read interactively and never appear in
// argv/env/files/logs; the capture is AES-256-GCM encrypted into a single
// 0600 file whose random passphrase is printed exactly once. Summary lines on
// stdout carry counts and outcomes only.
//
// The provider is driven through the same code path production uses
// (signing/providers/yiban), with a capturing http.RoundTripper injected via
// Config.Transport — the capture therefore shows what the new stack really
// sends, not a reimplementation.
package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
	yiban "github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/providers/yiban"
)

// sha256Sum 是一次性探针的 KDF 原语：stdlib 单实现，不为此引入 x/crypto。
func sha256Sum(b []byte) []byte {
	h := sha256.New()
	h.Write(b)
	return h.Sum(nil)
}

// captureRT 包住真实 Transport，逐跳记录线络。
type captureRT struct {
	mu      sync.Mutex
	inner   http.RoundTripper
	records []wireRecord
}

type wireRecord struct {
	Step        int               `json:"step"`
	Method      string            `json:"method"`
	URL         string            `json:"url"`
	ReqHeaders  map[string]string `json:"req_headers"`
	ReqBody     string            `json:"req_body"`
	Status      int               `json:"status"`
	RespHeaders map[string]string `json:"resp_headers"`
	RespBody    string            `json:"resp_body"`
	Err         string            `json:"error,omitempty"`
}

func (c *captureRT) RoundTrip(req *http.Request) (*http.Response, error) {
	var reqBody []byte
	if req.Body != nil {
		reqBody, _ = io.ReadAll(req.Body)
		req.Body = io.NopCloser(bytes.NewReader(reqBody))
	}
	rec := wireRecord{
		Method:     req.Method,
		URL:        req.URL.String(),
		ReqHeaders: flatten(req.Header),
		ReqBody:    string(reqBody),
	}
	resp, err := c.inner.RoundTrip(req)
	if err != nil {
		rec.Err = err.Error()
	} else {
		var respBody []byte
		respBody, err = io.ReadAll(resp.Body)
		if cerr := resp.Body.Close(); cerr != nil && err == nil {
			err = cerr
		}
		resp.Body = io.NopCloser(bytes.NewReader(respBody))
		rec.Status = resp.StatusCode
		rec.RespHeaders = flatten(resp.Header)
		rec.RespBody = string(respBody)
	}
	c.mu.Lock()
	rec.Step = len(c.records) + 1
	c.records = append(c.records, rec)
	c.mu.Unlock()
	return resp, err
}

func flatten(h http.Header) map[string]string {
	out := make(map[string]string, len(h))
	for k, v := range h {
		out[strings.ToLower(k)] = strings.Join(v, "\n")
	}
	return out
}

func readLine(prompt string) string {
	fmt.Print(prompt)
	sc := bufio.NewScanner(os.Stdin)
	sc.Scan()
	return strings.TrimSpace(sc.Text())
}

func encryptDump(records []wireRecord, passphrase, outPath string) error {
	salt := make([]byte, 16)
	nonce := make([]byte, 12)
	if _, err := rand.Read(salt); err != nil {
		return err
	}
	if _, err := rand.Read(nonce); err != nil {
		return err
	}
	key := scryptKey([]byte(passphrase), salt)
	payload, err := json.MarshalIndent(records, "", " ")
	if err != nil {
		return err
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return err
	}
	ct := gcm.Seal(nil, nonce, payload, nil)
	fd, err := os.OpenFile(outPath, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	defer func() { _ = fd.Close() }()
	_, err = fd.Write(append(append(append([]byte("YPG1"), salt...), nonce...), ct...))
	return err
}

// scryptKey 用 stdlib 可达的最简 KDF：SHA-256 迭代。探针是一次性件，
// 抓取物只在生产机落盘几小时；不为此引入 x/crypto 依赖。
func scryptKey(pass, salt []byte) []byte {
	out := sha256Sum(append(append([]byte("yiban-probe-go.v1"), salt...), pass...))
	for i := 1; i < 4096; i++ {
		out = sha256Sum(out)
	}
	return out
}

func main() {
	phone := readLine("测试账号手机号: ")
	password, err := readPassword("密码（不回显）: ")
	if err != nil {
		fatal("读取密码失败: %v", err)
	}
	if phone == "" || password == "" {
		fatal("凭据为空，退出")
	}

	cap := &captureRT{inner: http.DefaultTransport}
	provider := yiban.NewProvider(yiban.New(yiban.Config{
		Transport: cap,
		Clock:     clock.System{},
	}))
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()

	summary := "fail"
	_, verr := provider.Verify(ctx, contract.VerifyRequest{
		Account:    contract.AccountRef{ID: "probe", Platform: "yiban"},
		Credential: contract.Credential{Phone: phone, Password: password},
	})
	switch {
	case verr == nil:
		summary = "ok"
	case errors.Is(verr, context.DeadlineExceeded):
		summary = "timeout"
	default:
		summary = "error: " + verr.Error()
	}

	ts := clock.System{}.Now().Format("20060102-150405")
	outPath := "yiban-go-probe-" + ts + ".bin"
	passBytes := make([]byte, 16)
	if _, err := rand.Read(passBytes); err != nil {
		fatal("生成口令失败: %v", err)
	}
	passphrase := hex.EncodeToString(passBytes)
	if err := encryptDump(cap.records, passphrase, outPath); err != nil {
		fatal("加密落盘失败: %v", err)
	}

	fmt.Println("抓取条数:", len(cap.records))
	fmt.Println("登录:", summary)
	fmt.Println("抓取文件:", outPath)
	fmt.Println("解开口令（只显示这一次，抄下来）:", passphrase)
}

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "probe: "+format+"\n", args...)
	os.Exit(2)
}
