package legacy

// 旧凭据信封的解密（P8-B）。信封形状与 yiban/adapters/config/account_crypto.py
// 一致：v/kid/nonce/ct/tag 全十六进制（v1 无 kid，永久可读），
// AES-256-GCM，AAD＝手机号 UTF-8 字节，ct 与 tag 分离存放。
//
// 明文只在内存：本文件的返回值只进 CredentialPayload，不落盘、不进日志。

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"unicode/utf8"
)

// legacyKIDContext 是旧栈的 kid 派生域分隔串（account_crypto.KID_CONTEXT）。
const legacyKIDContext = "yiban-accounts-kid-v1"

// LegacyKID 计算旧钥指纹：HMAC-SHA256(key, 域分隔串) 前 8 字节的十六进制。
func LegacyKID(key []byte) string {
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(legacyKIDContext))
	return hex.EncodeToString(mac.Sum(nil)[:8])
}

// LegacyOpenEnvelope 解一个旧信封，返回明文（仅内存）。
//
// 失败显性：钥不匹配（v2 kid 先比对）、密文字段非法、AAD 不符、明文非
// UTF-8 都各自报错，绝不静默返回空串。
func LegacyOpenEnvelope(env map[string]any, phone string, key []byte) (string, error) {
	if len(key) != 32 {
		return "", fmt.Errorf("legacy credential key is %d bytes, want 32", len(key))
	}
	if env == nil {
		return "", errors.New("legacy credential envelope is missing")
	}
	ver, err := envelopeVersion(env["v"])
	if err != nil {
		return "", err
	}
	if ver == 2 {
		envelopeKID := valueString(env["kid"])
		mine := LegacyKID(key)
		if envelopeKID != mine {
			return "", fmt.Errorf("legacy credential key mismatch: envelope kid=%s, key kid=%s",
				orMissing(envelopeKID), mine)
		}
	}
	nonce, err := decodeHexField(env, "nonce", 12)
	if err != nil {
		return "", err
	}
	ct, err := decodeHexField(env, "ct", 0)
	if err != nil {
		return "", err
	}
	tag, err := decodeHexField(env, "tag", 16)
	if err != nil {
		return "", err
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return "", fmt.Errorf("legacy credential cipher: %w", err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return "", fmt.Errorf("legacy credential gcm: %w", err)
	}
	// Go 的 GCM 口径是 ct||tag 连续；旧栈两字段分离，这里拼回。
	plain, err := gcm.Open(nil, nonce, append(ct, tag...), []byte(phone))
	if err != nil {
		return "", errors.New("legacy credential cannot be decrypted " +
			"(wrong key, tampered ciphertext, or phone mismatch)")
	}
	if !utf8.Valid(plain) {
		return "", errors.New("legacy credential plaintext is not valid UTF-8")
	}
	return string(plain), nil
}

func envelopeVersion(v any) (int, error) {
	n, ok := valueInt(v)
	if !ok {
		return 0, fmt.Errorf("legacy credential version is not a number: %v", v)
	}
	if n != 1 && n != 2 {
		return 0, fmt.Errorf("unsupported legacy credential version: %d", n)
	}
	return int(n), nil
}

func decodeHexField(env map[string]any, name string, wantLen int) ([]byte, error) {
	raw, ok := env[name].(string)
	if !ok {
		return nil, fmt.Errorf("legacy credential field %q is missing or not a string", name)
	}
	b, err := hex.DecodeString(raw)
	if err != nil {
		return nil, fmt.Errorf("legacy credential field %q is not hex: %v", name, err)
	}
	if wantLen != 0 && len(b) != wantLen {
		return nil, fmt.Errorf("legacy credential field %q is %d bytes, want %d", name, len(b), wantLen)
	}
	return b, nil
}

func orMissing(s string) string {
	if s == "" {
		return "<missing>"
	}
	return s
}
