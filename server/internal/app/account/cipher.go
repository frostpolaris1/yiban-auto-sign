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
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"

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
	aead   cipher.AEAD
	kid    string
	random io.Reader
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
	return &Cipher{aead: aead, kid: hex.EncodeToString(mac.Sum(nil)[:8]), random: rand.Reader}, nil
}

// KID 返回本钥的标识。
func (c *Cipher) KID() string { return c.kid }

// Seal 加密一次凭据。Version 固定为 1，由仓储自增。
func (c *Cipher) Seal(schoolID, accountID string, payload CredentialPayload) (coreaccount.Credential, error) {
	if err := c.ready(); err != nil {
		return coreaccount.Credential{}, err
	}
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
	if err := c.ready(); err != nil {
		return CredentialPayload{}, err
	}
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

// SecretDomainMailPass 是 mail.pass 秘密串的 AAD 域标签。
// 同一把钥、不同域：凭据 AAD 是 school|account，秘密串 AAD 是固定标签，
// 两类密文互不可解。域标签是常量，调用方不得拼接用户输入。
const SecretDomainMailPass = "yiban.mail.pass.v1"

// SealSecret 用同一把钥加密任意敏感字符串，返回 base64(nonce||ciphertext)。
//
// 这是 account cipher 的通用秘密串扩展（SMTP 口令落库），
// **不引入第二套加密实现**：AEAD、nonce 长度与密钥来源全部复用本类型。
// 明文负载是裸字符串，不走凭据的三键 JSON；AAD 用调用方给的固定域标签。
// 返回的 base64 是 settings 行的存储形态，不是枚举词，读面不得按词表解析。
func (c *Cipher) SealSecret(domain, plaintext string) (string, error) {
	if err := c.ready(); err != nil {
		return "", err
	}
	if domain == "" {
		return "", errors.New("account: secret domain is required")
	}
	nonce := make([]byte, c.aead.NonceSize())
	if _, err := io.ReadFull(c.random, nonce); err != nil {
		return "", fmt.Errorf("account: read nonce: %w", err)
	}
	ciphertext := c.aead.Seal(nil, nonce, []byte(plaintext), []byte(domain))
	blob := make([]byte, 0, len(nonce)+len(ciphertext))
	blob = append(blob, nonce...)
	blob = append(blob, ciphertext...)
	return base64.StdEncoding.EncodeToString(blob), nil
}

// OpenSecret 解密 SealSecret 的输出。
// 坏 base64、截断、域标签不符与密文篡改一律报错：
// **绝不静默返回空串**，否则坏行会被读成「未设置」。
func (c *Cipher) OpenSecret(domain, blob string) (string, error) {
	if err := c.ready(); err != nil {
		return "", err
	}
	if domain == "" {
		return "", errors.New("account: secret domain is required")
	}
	raw, err := base64.StdEncoding.DecodeString(blob)
	if err != nil {
		return "", errors.New("account: secret blob is not base64")
	}
	if len(raw) < c.aead.NonceSize()+c.aead.Overhead() {
		return "", errors.New("account: secret blob is truncated")
	}
	nonce := raw[:c.aead.NonceSize()]
	ciphertext := raw[c.aead.NonceSize():]
	plain, err := c.aead.Open(nil, nonce, ciphertext, []byte(domain))
	if err != nil {
		return "", errors.New("account: secret cannot be decrypted")
	}
	return string(plain), nil
}

// ready 报告 cipher 是否可用。零值或半构造的 cipher 返回错误而不是 panic：
// 装配缺失必须显性失败，不得崩进程。
func (c *Cipher) ready() error {
	if c == nil || c.aead == nil || c.random == nil {
		return errors.New("account: cipher is not initialized")
	}
	return nil
}
