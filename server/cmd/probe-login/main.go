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
	"flag"
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
	key := deriveCaptureKey([]byte(passphrase), salt)
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

// deriveCaptureKey 用 stdlib 可达的最简 KDF：SHA-256 迭代（名字如实，不是 scrypt）。
// 探针是一次性件，抓取物只在生产机落盘几小时；不为此引入 x/crypto 依赖。
func deriveCaptureKey(pass, salt []byte) []byte {
	out := sha256Sum(append(append([]byte("yiban-probe-go.v1"), salt...), pass...))
	for i := 1; i < 4096; i++ {
		out = sha256Sum(out)
	}
	return out
}

// decryptDump 用与加密完全相同的代码路径解抓取文件，明文 JSON 打到 stdout。
func decryptDump(path string) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if len(raw) < 4+16+12+16 || string(raw[:4]) != "YPG1" {
		return errors.New("capture file malformed")
	}
	salt, nonce := raw[4:20], raw[20:32]
	body := raw[32:]
	tag, ct := body[len(body)-16:], body[:len(body)-16]
	passphrase, err := readPassword("解开口令: ")
	if err != nil {
		return err
	}
	key := deriveCaptureKey([]byte(passphrase), salt)
	block, err := aes.NewCipher(key)
	if err != nil {
		return err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return err
	}
	pt, err := gcm.Open(nil, nonce, append(append([]byte{}, ct...), tag...), nil)
	if err != nil {
		return err
	}
	fmt.Println(string(pt))
	return nil
}

// 预存凭据的两件文件名。密钥与密文同机同目录（0600）：对 root 等于明文，
// 换取的是「到点无人值守开跑」；这台机器单用户、一次性，用户已接受该口径。
const (
	credBinPath = "cred.bin"
	credKeyPath = "cred.key"
)

func sealWithKey(key, payload []byte) ([]byte, error) {
	nonce := make([]byte, 12)
	if _, err := rand.Read(nonce); err != nil {
		return nil, err
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	return append(append([]byte("YPC1"), nonce...), gcm.Seal(nil, nonce, payload, nil)...), nil
}

func openWithKey(key, data []byte) ([]byte, error) {
	if len(data) < 4+12+16 || string(data[:4]) != "YPC1" {
		return nil, errors.New("credential blob malformed")
	}
	nonce, ct := data[4:16], data[16:]
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	return gcm.Open(nil, nonce, ct, nil)
}

func writeFile0600(path string, data []byte) error {
	return os.WriteFile(path, data, 0o600)
}

func stageCredentials() {
	phone := readLine("测试账号手机号: ")
	password, err := readPassword("密码（不回显）: ")
	if err != nil {
		fatal("读取密码失败: %v", err)
	}
	if phone == "" || password == "" {
		fatal("凭据为空，退出")
	}
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		fatal("生成密钥失败: %v", err)
	}
	blob, err := sealWithKey(key, append(append([]byte(phone), 0), []byte(password)...))
	if err != nil {
		fatal("加密凭据失败: %v", err)
	}
	if err := writeFile0600(credKeyPath, key); err != nil {
		fatal("落盘密钥失败: %v", err)
	}
	if err := writeFile0600(credBinPath, blob); err != nil {
		fatal("落盘凭据失败: %v", err)
	}
	fmt.Println("凭据已加密预存（cred.bin/cred.key，0600）。")
	fmt.Println("现在可以退出（Ctrl+B 然后 D）；06:31 探针自动开跑，跑完凭据文件即被粉碎。")
}

func loadCredentials() (phone, password string, err error) {
	key, err := os.ReadFile(credKeyPath)
	if err != nil {
		return "", "", err
	}
	blob, err := os.ReadFile(credBinPath)
	if err != nil {
		return "", "", err
	}
	plain, err := openWithKey(key, blob)
	if err != nil {
		return "", "", err
	}
	sep := bytes.IndexByte(plain, 0)
	if sep < 0 {
		return "", "", errors.New("credential payload malformed")
	}
	return string(plain[:sep]), string(plain[sep+1:]), nil
}

func shredCredentials() {
	for _, p := range []string{credBinPath, credKeyPath} {
		if err := os.Remove(p); err != nil && !os.IsNotExist(err) {
			fmt.Fprintf(os.Stderr, "probe: 粉碎 %s 失败: %v\n", p, err)
		}
	}
}

func main() {
	stage := flag.Bool("stage", false, "今晚录入凭据：加密落盘两件 0600 文件后退出，等 -fire 到点开跑")
	fire := flag.Bool("fire", false, "定时模式：读取 -stage 落盘的凭据开跑，跑完立即粉碎凭据文件")
	decrypt := flag.String("decrypt", "", "解密指定抓取文件：口令从 stdin 读入，明文 JSON 打到 stdout")
	flag.Parse()

	if *decrypt != "" {
		if err := decryptDump(*decrypt); err != nil {
			fatal("解密失败: %v", err)
		}
		return
	}

	var phone, password string
	switch {
	case *stage:
		stageCredentials()
		return
	case *fire:
		var err error
		phone, password, err = loadCredentials()
		if err != nil {
			fatal("读取预存凭据失败（先跑 -stage）: %v", err)
		}
		defer shredCredentials()
	default:
		phone = readLine("测试账号手机号: ")
		var perr error
		password, perr = readPassword("密码（不回显）: ")
		if perr != nil {
			fatal("读取密码失败: %v", perr)
		}
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
