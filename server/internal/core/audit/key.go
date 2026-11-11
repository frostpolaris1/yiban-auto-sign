package audit

// 审计链密钥派生。
//
// P1 没有独立的 YIBAN_AUDIT_KEY；派生值稳定，重启后仍可校验旧链。
// 轮换 pepper 会让旧链无法再校验，需要一次迁移口径。
// 域标签把本用途与 CSRF、IP 摘要隔开：三者消息不同。
// 凭据加密钥**不**在此派生：它由 YIBAN_CREDENTIAL_KEY 独立注入。

import (
	"crypto/hmac"
	"crypto/sha256"
	"errors"
)

// DeriveKey 从服务器 pepper 派生审计链密钥。
// 输出是 HMAC-SHA256(pepper, "yiban.audit.key.v1") 的原始 32 字节。
func DeriveKey(pepper []byte) ([]byte, error) {
	if len(pepper) == 0 {
		return nil, errors.New("audit: YIBAN_HASH_PEPPER is required to derive the audit key")
	}
	mac := hmac.New(sha256.New, pepper)
	mac.Write([]byte("yiban.audit.key.v1"))
	return mac.Sum(nil), nil
}
