package account

// 本文件实现账号凭据的 AES-256-GCM 加解密。
//
// # 口径
// 密钥 32 字节；nonce 12 字节，每次写入重新生成。
// AAD 固定为 school_id + "|" + account_id，两个 UUID 都是小写带连字符形式。
// kid = HMAC-SHA256(key, "yiban.kid.v1") 前 8 字节的十六进制。
// 明文负载是固定三键 JSON：device_code、password、phone_model，全部出现。
//
// **本文件不读环境变量、不写日志、不做轮换**。钥由装配方注入；
// 轮换与多钥读取不属于 P1。

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
)

const (
	// CredentialAlg 是凭据行允许的唯一算法，与 ck_credentials_alg 同值。
	CredentialAlg = "AES-256-GCM"
	// CredentialAADVersion 是 AAD 口径版本。
	CredentialAADVersion int16 = 1
	// CredentialKeyBytes 是 AES-256 密钥长度。
	CredentialKeyBytes = 32
	// credentialKIDDomain 把 kid 派生与其他 HMAC 用途隔开。
	credentialKIDDomain = "yiban.kid.v1"
)

// CredentialPayload 是密文解密后的明文负载。
// 三键**全部出现**，缺键即解码失败；未知键也被拒绝。
type CredentialPayload struct {
	DeviceCode string `json:"device_code"`
	Password   string `json:"password"`
	PhoneModel string `json:"phone_model"`
}

// Cipher 是单钥的凭据加解密器。
// 零值不可用，请用 NewCipher 构造。
type Cipher struct {
	aead cipher.AEAD
	kid  string
}

// NewCipher 校验密钥长度并派生 kid。
// 长度不是 32 字节即失败，**不补齐、不推导**。
func NewCipher(key []byte) (*Cipher, error) {
	if len(key) != CredentialKeyBytes {
		return nil, fmt.Errorf("account: credential key is %d bytes, want %d", len(key), CredentialKeyBytes)
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, fmt.Errorf("account: create cipher: %w", err)
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, fmt.Errorf("account: create gcm: %w", err)
	}
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(credentialKIDDomain))
	return &Cipher{aead: aead, kid: hex.EncodeToString(mac.Sum(nil)[:8])}, nil
}

// KID 返回本钥的标识。
func (c *Cipher) KID() string { return c.kid }

// Seal 加密一次凭据。Version 固定为 1，由仓储自增。
func (c *Cipher) Seal(schoolID, accountID string, payload CredentialPayload) (coreaccount.Credential, error) {
	plain, err := json.Marshal(payload)
	if err != nil {
		return coreaccount.Credential{}, fmt.Errorf("account: encode credential: %w", err)
	}
	nonce := make([]byte, c.aead.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return coreaccount.Credential{}, fmt.Errorf("account: read nonce: %w", err)
	}
	ciphertext := c.aead.Seal(nil, nonce, plain, aad(schoolID, accountID))
	return coreaccount.Credential{
		AccountID:  accountID,
		SchoolID:   schoolID,
		Alg:        CredentialAlg,
		KID:        c.kid,
		AADVersion: CredentialAADVersion,
		Nonce:      nonce,
		Ciphertext: ciphertext,
		Version:    1,
	}, nil
}

// Open 用行内 kid 校验并解密。
// kid 不是本钥即失败：**绝不静默返回空负载**。
func (c *Cipher) Open(cred coreaccount.Credential) (CredentialPayload, error) {
	if cred.KID != c.kid {
		return CredentialPayload{}, errors.New("account: credential key id is not available")
	}
	if cred.AADVersion != CredentialAADVersion {
		return CredentialPayload{}, fmt.Errorf("account: unsupported aad version %d", cred.AADVersion)
	}
	// AEAD.Open 对错误长度的 nonce 会 panic；列 CHECK 保证 12 字节，
	// 这里再挡一次，让坏行变成错误而不是崩进程。
	if len(cred.Nonce) != c.aead.NonceSize() {
		return CredentialPayload{}, fmt.Errorf("account: nonce is %d bytes, want %d", len(cred.Nonce), c.aead.NonceSize())
	}
	plain, err := c.aead.Open(nil, cred.Nonce, cred.Ciphertext, aad(cred.SchoolID, cred.AccountID))
	if err != nil {
		return CredentialPayload{}, errors.New("account: credential cannot be decrypted")
	}
	var payload CredentialPayload
	decoder := json.NewDecoder(bytes.NewReader(plain))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&payload); err != nil {
		return CredentialPayload{}, fmt.Errorf("account: decode credential: %w", err)
	}
	return payload, nil
}

// aad 固定 AAD 的逐字节口径。
func aad(schoolID, accountID string) []byte {
	return []byte(schoolID + "|" + accountID)
}
