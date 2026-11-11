package main

// SMTP 设置写回（smtp4）在真装配上的 e2e。
//
// 断言：
//  1. GET 缺行回 env/registry 值；PUT 六键后 GET 报 source=db，
//     敏感位只回 set/掩码，无明文与密文。
//  2. mail.pass 落 settings 行是 AES-GCM 密文（base64），不含明文，
//     能被同一把 account cipher 解回。
//  3. 每发送解析：PUT 新 host/port/user/pass 后下一次 POST mail/test
//     命中新假 SMTP 服务器；null 删行后下一次回 env 服务器（非重启）。
//  4. 审计恰一行 settings.mail_writeback；detail 无明文/密文口令，
//     敏感键只记 set/cleared。
//  5. current_password 缺失 400、错误 403，均零写入零审计；
//     域负例 400 零写入零审计。
//  6. 审计失败整体回滚：覆盖行不落地，PUT 回 500。

import (
	"context"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/mail/mailtest"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// mailSettingsBody 是 GET/PUT 响应的解析形状。
type mailSettingsBody struct {
	Host struct {
		Value  string `json:"value"`
		Source string `json:"source"`
	} `json:"host"`
	Port struct {
		Value  int    `json:"value"`
		Source string `json:"source"`
	} `json:"port"`
	TLSMode struct {
		Value  string `json:"value"`
		Source string `json:"source"`
	} `json:"tls_mode"`
	From struct {
		Set    bool   `json:"set"`
		Masked string `json:"masked"`
		Source string `json:"source"`
	} `json:"from"`
	User struct {
		Set    bool   `json:"set"`
		Masked string `json:"masked"`
		Source string `json:"source"`
	} `json:"user"`
	Pass struct {
		Set    bool   `json:"set"`
		Source string `json:"source"`
	} `json:"pass"`
}

func getMailSettings(t *testing.T, e *authEnv) (*http.Response, []byte, mailSettingsBody) {
	t.Helper()
	resp, payload := e.do(http.MethodGet, "/api/admin/mail-settings", nil, nil)
	var body mailSettingsBody
	if resp.StatusCode == http.StatusOK {
		if err := json.Unmarshal(payload, &body); err != nil {
			t.Fatalf("decode GET mail-settings: %v; body=%q", err, payload)
		}
	}
	return resp, payload, body
}

func putMailSettings(t *testing.T, e *authEnv, csrf, body string) (*http.Response, []byte, mailSettingsBody) {
	t.Helper()
	resp, payload := e.do(http.MethodPut, "/api/admin/mail-settings",
		json.RawMessage(body), map[string]string{"X-CSRF-Token": csrf})
	var report mailSettingsBody
	if resp.StatusCode == http.StatusOK {
		if err := json.Unmarshal(payload, &report); err != nil {
			t.Fatalf("decode PUT mail-settings: %v; body=%q", err, payload)
		}
	}
	return resp, payload, report
}

// mailEnvConfigFor 把装配配置指向 env 假服务器。
func mailEnvConfigFor(port int) func(*config.Config) {
	return func(cfg *config.Config) {
		cfg.ResetMailEnabled = true
		cfg.MailHost = "127.0.0.1"
		cfg.MailPort = port
		cfg.MailUser = e2eMailUser
		cfg.MailPass = e2eMailPass
		cfg.MailFrom = e2eMailUser
		cfg.MailTLSMode = "starttls"
		cfg.PublicBaseURL = e2eBaseURL
		// 两台进程内服务器各自自签；测试用跳过校验共享信任。
		// 生产 MailTLSConfig 恒为 nil，不提供该开关。
		cfg.MailTLSConfig = &tls.Config{InsecureSkipVerify: true} //nolint:gosec // 进程内自签测试服务器
	}
}

// createMailMaster 建一个 master 并登录，返回 CSRF。
// 测试邮件冷却按管理员 id 计：两次 POST mail/test 用两个 master，
// 避免第二次被 60s 冷却挡住（与投递路径无关）。
func createMailMaster(t *testing.T, e *authEnv, email, password string) string {
	t.Helper()
	hash, err := coreauth.HashPassword(password)
	if err != nil {
		t.Fatalf("hash master password: %v", err)
	}
	err = e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.users.Create(ctx, coreuser.New{
			Email: email, PasswordHash: hash, Role: coreauth.RoleMaster,
		}, 10000)
		return err
	})
	if err != nil {
		t.Fatalf("create master %s: %v", email, err)
	}
	resp, body := e.login(email, password)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("login %s = %d, want 200", email, resp.StatusCode)
	}
	return body.CSRFToken
}

// 六键写回、密文落库、每发送解析与审计的整链。
func TestMailSettingsWritebackAndPerSendResolution(t *testing.T) {
	envSrv, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start env fake smtp: %v", err)
	}
	t.Cleanup(envSrv.Close)
	envSrv.SetCredentials(e2eMailUser, e2eMailPass)
	nextSrv, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start next fake smtp: %v", err)
	}
	t.Cleanup(nextSrv.Close)
	const nextUser = "next-mailer@example.test"
	const nextPass = "next-smtp-secret-7c31"
	nextSrv.SetCredentials(nextUser, nextPass)

	// 让 env 来源可判定：装配端用 config.Env 判 env/default。
	// 必须在 newAuthEnvWith 之前设置：mailResolver 在装配时定型 env 视图。
	t.Setenv("YIBAN_MAIL_SMTP_HOST", "127.0.0.1")
	t.Setenv("YIBAN_MAIL_SMTP_PORT", strconv.Itoa(envSrv.Port()))
	t.Setenv("YIBAN_MAIL_TLS_MODE", "starttls")
	t.Setenv("YIBAN_MAIL_USER", e2eMailUser)
	t.Setenv("YIBAN_MAIL_PASS", e2eMailPass)
	t.Setenv("YIBAN_MAIL_FROM", e2eMailUser)
	e := newAuthEnvWith(t, mailEnvConfigFor(envSrv.Port()), nil)
	csrf := s05Master(t, e)

	// GET：缺行回 env 值，pass 只报 set。
	resp, payload, report := getMailSettings(t, e)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET mail-settings = %d; body=%q", resp.StatusCode, payload)
	}
	if report.Host.Value != "127.0.0.1" || report.Port.Value != envSrv.Port() ||
		report.TLSMode.Value != "starttls" {
		t.Fatalf("env report = %+v", report)
	}
	if !report.From.Set || report.From.Masked != "****" || !report.User.Set || !report.Pass.Set {
		t.Fatalf("sensitive report = %+v", report)
	}
	if report.Pass.Source != "env" || report.Host.Source != "env" {
		t.Fatalf("sources = host %q pass %q, want env", report.Host.Source, report.Pass.Source)
	}

	// PUT 六键：新服务器 + 新凭据。
	putBody := `{"host":"127.0.0.1","port":` + strconv.Itoa(nextSrv.Port()) +
		`,"tls_mode":"starttls","from":"` + nextUser + `","user":"` + nextUser +
		`","pass":"` + nextPass + `","current_password":"master-password-9!"}`
	resp, payload, report = putMailSettings(t, e, csrf, putBody)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("PUT mail-settings = %d; body=%q", resp.StatusCode, payload)
	}
	if report.Host.Source != "db" || report.Port.Source != "db" ||
		report.From.Source != "db" || report.User.Source != "db" || report.Pass.Source != "db" {
		t.Fatalf("PUT sources = %+v", report)
	}
	if report.Port.Value != nextSrv.Port() || !report.Pass.Set {
		t.Fatalf("PUT report = %+v", report)
	}
	if strings.Contains(string(payload), nextPass) {
		t.Fatal("PUT response contains the SMTP password")
	}

	// 密文落库：不含明文，base64 可解，account cipher 能解回。
	stored := e.scalarString("SELECT value FROM settings WHERE key = 'mail.pass'")
	if stored == "" || stored == nextPass || strings.Contains(stored, nextPass) {
		t.Fatalf("mail.pass row is not encrypted: %q", stored)
	}
	if _, err := base64.StdEncoding.DecodeString(stored); err != nil {
		t.Fatalf("mail.pass row is not base64: %v", err)
	}
	cipher, err := appaccount.NewCipher(authTestCredentialKey)
	if err != nil {
		t.Fatalf("NewCipher: %v", err)
	}
	plain, err := cipher.OpenSecret(appaccount.SecretDomainMailPass, stored)
	if err != nil || plain != nextPass {
		t.Fatalf("account cipher cannot open mail.pass: (%q, %v)", plain, err)
	}

	// 审计恰一行；detail 无明文/密文，敏感键只记 set/cleared。
	if n := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'settings.mail_writeback'"); n != 1 {
		t.Fatalf("mail_writeback audit rows = %d, want 1", n)
	}
	detail := e.scalarString("SELECT detail FROM audit_logs WHERE action = 'settings.mail_writeback'")
	if strings.Contains(detail, nextPass) || strings.Contains(detail, stored) {
		t.Fatalf("audit detail contains the password or ciphertext: %s", detail)
	}
	var detailBody struct {
		Changes map[string][2]any `json:"changes"`
	}
	if err := json.Unmarshal([]byte(detail), &detailBody); err != nil {
		t.Fatalf("decode audit detail: %v; detail=%s", err, detail)
	}
	if pair := detailBody.Changes["mail.pass"]; pair[0] != "cleared" || pair[1] != "set" {
		t.Fatalf("mail.pass audit pair = %#v, want [cleared set]", pair)
	}
	if pair := detailBody.Changes["mail.smtp_host"]; pair[0] != nil || pair[1] != "127.0.0.1" {
		t.Fatalf("mail.smtp_host audit pair = %#v", pair)
	}

	// 每发送解析：下一次 POST mail/test 命中新服务器与新凭据。
	resp, payload = e.do(http.MethodPost, "/api/admin/mail/test",
		map[string]string{"current_password": "master-password-9!"},
		map[string]string{"X-CSRF-Token": csrf})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("POST mail/test after PUT = %d; body=%q", resp.StatusCode, payload)
	}
	if !nextSrv.WaitMessages(1, 5*time.Second) {
		t.Fatal("next SMTP server did not receive the test mail")
	}
	if envSrv.MessageCount() != 0 {
		t.Fatalf("env SMTP server received %d messages after the write", envSrv.MessageCount())
	}
	if got := nextSrv.Messages()[0].AuthUser; got != nextUser {
		t.Fatalf("AUTH user = %q, want the DB override %q", got, nextUser)
	}

	// null 删行：下一次外呼回 env 服务器与 env 凭据。
	// 第二次 POST mail/test 用另一个 master，避开每管理员 60s 冷却。
	csrf2 := createMailMaster(t, e, "smtp4-second-master@example.com", "master-password-9!")
	clearBody := `{"host":null,"port":null,"tls_mode":null,"from":null,"user":null,"pass":null,` +
		`"current_password":"master-password-9!"}`
	resp, payload, report = putMailSettings(t, e, csrf2, clearBody)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("PUT clear = %d; body=%q", resp.StatusCode, payload)
	}
	if report.Host.Source != "env" || report.Port.Source != "env" || report.Pass.Source != "env" {
		t.Fatalf("cleared sources = %+v", report)
	}
	if n := e.scalarInt("SELECT count(*) FROM settings WHERE key LIKE 'mail.%'"); n != 0 {
		t.Fatalf("mail settings rows survived the clear: %d", n)
	}
	resp, payload = e.do(http.MethodPost, "/api/admin/mail/test",
		map[string]string{"current_password": "master-password-9!"},
		map[string]string{"X-CSRF-Token": csrf2})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("POST mail/test after clear = %d; body=%q", resp.StatusCode, payload)
	}
	if !envSrv.WaitMessages(1, 5*time.Second) {
		t.Fatal("env SMTP server did not receive the test mail after the clear")
	}
	if got := envSrv.Messages()[0].AuthUser; got != e2eMailUser {
		t.Fatalf("AUTH user after clear = %q, want env %q", got, e2eMailUser)
	}
	// 第二次 PUT 也恰一行审计，敏感键旧态是 set、新态是 cleared。
	if n := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'settings.mail_writeback'"); n != 2 {
		t.Fatalf("mail_writeback audit rows = %d, want 2", n)
	}
}

// 二次口令与域校验：缺失 400、错误 403、域负例 400，均零写入零审计。
func TestMailSettingsRejectsWithoutWrite(t *testing.T) {
	envSrv, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start fake smtp: %v", err)
	}
	t.Cleanup(envSrv.Close)
	envSrv.SetCredentials(e2eMailUser, e2eMailPass)
	e := newAuthEnvWith(t, mailEnvConfigFor(envSrv.Port()), nil)
	csrf := s05Master(t, e)

	cases := []struct {
		name   string
		body   string
		status int
	}{
		{"missing password", `{"host":"127.0.0.1"}`, http.StatusBadRequest},
		{"empty password", `{"host":"127.0.0.1","current_password":""}`, http.StatusBadRequest},
		{"wrong password", `{"host":"127.0.0.1","current_password":"wrong-password"}`, http.StatusForbidden},
		{"port too high", `{"port":65536,"current_password":"master-password-9!"}`, http.StatusBadRequest},
		{"port negative", `{"port":-1,"current_password":"master-password-9!"}`, http.StatusBadRequest},
		{"port not int", `{"port":"587","current_password":"master-password-9!"}`, http.StatusBadRequest},
		{"tls unknown", `{"tls_mode":"ssl","current_password":"master-password-9!"}`, http.StatusBadRequest},
		{"host empty", `{"host":"","current_password":"master-password-9!"}`, http.StatusBadRequest},
		{"pass empty", `{"pass":"","current_password":"master-password-9!"}`, http.StatusBadRequest},
		{"unknown field", `{"smtp":"x","current_password":"master-password-9!"}`, http.StatusBadRequest},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			resp, payload, _ := putMailSettings(t, e, csrf, tc.body)
			if resp.StatusCode != tc.status {
				t.Fatalf("status = %d, want %d; body=%q", resp.StatusCode, tc.status, payload)
			}
			if n := e.scalarInt("SELECT count(*) FROM settings WHERE key LIKE 'mail.%'"); n != 0 {
				t.Fatalf("rejected write left %d rows", n)
			}
			if n := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'settings.mail_writeback'"); n != 0 {
				t.Fatalf("rejected write left %d audit rows", n)
			}
		})
	}
}

// 审计失败整体回滚：覆盖行不落地，PUT 回 500。
func TestMailSettingsAuditFailureRollsBack(t *testing.T) {
	envSrv, err := mailtest.Start(mailtest.StartTLS)
	if err != nil {
		t.Fatalf("start fake smtp: %v", err)
	}
	t.Cleanup(envSrv.Close)
	envSrv.SetCredentials(e2eMailUser, e2eMailPass)
	e := newAuthEnvWith(t, mailEnvConfigFor(envSrv.Port()), nil)
	csrf := s05Master(t, e)

	unblock := blockAuditInserts(t, e)
	resp, payload, _ := putMailSettings(t, e, csrf,
		`{"host":"127.0.0.1","current_password":"master-password-9!"}`)
	unblock()
	if resp.StatusCode != http.StatusInternalServerError {
		t.Fatalf("PUT with blocked audit = %d, want 500; body=%q", resp.StatusCode, payload)
	}
	if n := e.scalarInt("SELECT count(*) FROM settings WHERE key = 'mail.smtp_host'"); n != 0 {
		t.Fatalf("override row survived the audit failure: %d rows", n)
	}
}
