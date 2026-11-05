// 更新状态文件与密钥指纹。
//
// # 功能
// 派生域分离的 SHA-256 指纹（YIBAN_CREDENTIAL_KEY、YIBAN_HASH_PEPPER），
// 读写 /var/lib/yiban/update-state.json。
// 本文件**不**打印密钥原文，也不把指纹当密钥：指纹只用于检测变化。
//
// # 归属
// selfcheck 包内状态面。FileSystem 由调用方注入。
//
// # 口径
// 状态文件 schema：schema_version、key_fingerprints、recorded_at。
// 未知顶层字段与未知指纹名**保留**（U2 会加 last_update 等字段，
// 旧二进制不得把它们抹掉）；schema_version 高于本工具支持值时**拒绝**读写，
// 绝不降级覆盖。
// 指纹口径固定为 sha256(域 + 0x00 + 材料)：
//   - credential_key 的材料是 base64 解码后的 32 字节；
//   - hash_pepper 的材料是去空白后的原文（服务端同样把原文当 HMAC 密钥）。
//
// 域字符串一经发布不得修改：改域会把所有既有状态判成「指纹变化」。
package selfcheck

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"
)

// 指纹名与域字符串。
const (
	// FingerprintCredentialKey 是凭据钥的指纹名。
	FingerprintCredentialKey = "credential_key"
	// FingerprintHashPepper 是哈希 pepper 的指纹名。
	FingerprintHashPepper = "hash_pepper"

	fingerprintDomainCredentialKey = "yiban-update-state/v1/credential_key"
	fingerprintDomainHashPepper    = "yiban-update-state/v1/hash_pepper"
)

// State 是更新状态文件的内容。
type State struct {
	// SchemaVersion 是状态文件版本；当前只接受 1。
	SchemaVersion int
	// KeyFingerprints 以指纹名映射 sha256:<64hex>。
	KeyFingerprints map[string]string
	// RecordedAt 是最近一次记录时刻（RFC3339 UTC）。
	RecordedAt string

	// extra 保存未知顶层字段，写回时原样带上。
	extra map[string]json.RawMessage
}

// CredentialKeyFingerprint 派生凭据钥指纹。形状不合法即报错，不记录坏值。
func CredentialKeyFingerprint(raw string) (string, error) {
	material, err := credentialKeyMaterial(raw)
	if err != nil {
		return "", err
	}
	return fingerprint(fingerprintDomainCredentialKey, material), nil
}

// HashPepperFingerprint 派生 pepper 指纹。长度不足即报错。
func HashPepperFingerprint(raw string) (string, error) {
	material, err := pepperMaterial(raw)
	if err != nil {
		return "", err
	}
	return fingerprint(fingerprintDomainHashPepper, material), nil
}

// KeyFingerprints 派生两枚指纹；任一不合法即整体失败。
func KeyFingerprints(opts Options) (map[string]string, error) {
	credential, err := CredentialKeyFingerprint(opts.CredentialKey)
	if err != nil {
		return nil, err
	}
	pepper, err := HashPepperFingerprint(opts.HashPepper)
	if err != nil {
		return nil, err
	}
	return map[string]string{
		FingerprintCredentialKey: credential,
		FingerprintHashPepper:    pepper,
	}, nil
}

// credentialKeyMaterial 返回凭据钥的解码字节，与服务端 config 同口径。
func credentialKeyMaterial(raw string) ([]byte, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, errors.New("YIBAN_CREDENTIAL_KEY 未设置")
	}
	key, err := base64.StdEncoding.DecodeString(raw)
	if err != nil {
		return nil, errors.New("YIBAN_CREDENTIAL_KEY 不是合法 base64")
	}
	if len(key) != 32 {
		return nil, fmt.Errorf("YIBAN_CREDENTIAL_KEY 解码后 %d 字节，需要 32", len(key))
	}
	return key, nil
}

// pepperMaterial 返回 pepper 的原文字节，与服务端 config 同口径。
func pepperMaterial(raw string) ([]byte, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, errors.New("YIBAN_HASH_PEPPER 未设置")
	}
	if len(raw) < 16 {
		return nil, fmt.Errorf("YIBAN_HASH_PEPPER 只有 %d 字符，至少 16", len(raw))
	}
	return []byte(raw), nil
}

// fingerprint 计算域分离的 SHA-256 指纹。
func fingerprint(domain string, material []byte) string {
	h := sha256.New()
	_, _ = h.Write([]byte(domain))
	_, _ = h.Write([]byte{0})
	_, _ = h.Write(material)
	return "sha256:" + hex.EncodeToString(h.Sum(nil))
}

// ShortFingerprint 把指纹截成可打印前缀。
// 非 sha256:<64 位小写 hex> 的值只显示 <invalid>：绝不回显原文。
func ShortFingerprint(fp string) string {
	if !validFingerprint(fp) {
		return "<invalid>"
	}
	return fp[:len(fingerprintScheme)+8] + "…"
}

// fingerprintScheme 是指纹的固定前缀。
const fingerprintScheme = "sha256:"

// validFingerprint 校验 sha256:<64 位小写 hex>。
func validFingerprint(fp string) bool {
	if !strings.HasPrefix(fp, fingerprintScheme) {
		return false
	}
	hexPart := fp[len(fingerprintScheme):]
	if len(hexPart) != 64 {
		return false
	}
	for i := 0; i < len(hexPart); i++ {
		c := hexPart[i]
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return true
}

// LoadState 读状态文件。exists=false 表示文件不存在（不是错误）。
func LoadState(fsys FileSystem, path string) (state State, exists bool, err error) {
	body, err := fsys.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return State{}, false, nil
	}
	if err != nil {
		return State{}, false, fmt.Errorf("读取状态文件 %s: %w", path, err)
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil {
		return State{}, false, fmt.Errorf("状态文件 %s 不是合法 JSON: %w", path, err)
	}
	rawVersion, ok := raw["schema_version"]
	if !ok {
		return State{}, false, fmt.Errorf("状态文件 %s 缺 schema_version", path)
	}
	var version int
	if err := json.Unmarshal(rawVersion, &version); err != nil {
		return State{}, false, fmt.Errorf("状态文件 %s 的 schema_version 不是整数", path)
	}
	if version != StateSchemaVersion {
		return State{}, false, fmt.Errorf(
			"状态文件 %s 的 schema_version=%d，本工具只支持 %d；拒绝降级覆盖",
			path, version, StateSchemaVersion)
	}
	st := State{SchemaVersion: version, extra: map[string]json.RawMessage{}}
	if rawFP, ok := raw["key_fingerprints"]; ok && string(rawFP) != "null" {
		if err := json.Unmarshal(rawFP, &st.KeyFingerprints); err != nil {
			return State{}, false, fmt.Errorf("状态文件 %s 的 key_fingerprints 形状不对", path)
		}
	}
	for name, value := range st.KeyFingerprints {
		if !validFingerprint(value) {
			return State{}, false, fmt.Errorf(
				"状态文件 %s 的 key_fingerprints[%q] 不是 sha256:<64hex>；拒绝回显该值", path, name)
		}
	}
	if rawAt, ok := raw["recorded_at"]; ok && string(rawAt) != "null" {
		if err := json.Unmarshal(rawAt, &st.RecordedAt); err != nil {
			return State{}, false, fmt.Errorf("状态文件 %s 的 recorded_at 不是字符串", path)
		}
	}
	for key, value := range raw {
		switch key {
		case "schema_version", "key_fingerprints", "recorded_at":
		default:
			st.extra[key] = value
		}
	}
	return st, true, nil
}

// SaveState 原子写状态文件（0600）。未知字段原样保留。
func SaveState(fsys FileSystem, path string, st State) error {
	if st.SchemaVersion != StateSchemaVersion {
		return fmt.Errorf("updatecheck: 拒绝写 schema_version=%d（只写 %d）", st.SchemaVersion, StateSchemaVersion)
	}
	doc := make(map[string]json.RawMessage, len(st.extra)+3)
	for key, value := range st.extra {
		doc[key] = value
	}
	fps := st.KeyFingerprints
	if fps == nil {
		fps = map[string]string{}
	}
	for name, value := range fps {
		if !validFingerprint(value) {
			return fmt.Errorf("updatecheck: 拒绝写非法指纹 key_fingerprints[%q]", name)
		}
	}
	encoded, err := marshalField(st.SchemaVersion)
	if err != nil {
		return err
	}
	doc["schema_version"] = encoded
	if encoded, err = marshalField(fps); err != nil {
		return err
	}
	doc["key_fingerprints"] = encoded
	if encoded, err = marshalField(st.RecordedAt); err != nil {
		return err
	}
	doc["recorded_at"] = encoded
	body, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return fmt.Errorf("updatecheck: 编码状态文件: %w", err)
	}
	body = append(body, '\n')
	if err := fsys.WriteFileAtomic(path, body, 0o600); err != nil {
		return fmt.Errorf("写状态文件 %s: %w", path, err)
	}
	return nil
}

// marshalField 编码一个必然可序列化的字段。
func marshalField(v any) (json.RawMessage, error) {
	body, err := json.Marshal(v)
	if err != nil {
		return nil, fmt.Errorf("updatecheck: 编码状态字段: %w", err)
	}
	return body, nil
}

// RecordKeys 把当前密钥指纹写入状态文件。U2 在更新成功后显式调用。
// 只改 key_fingerprints 与 recorded_at，保留其余字段。
func RecordKeys(fsys FileSystem, path string, opts Options, now time.Time) (State, error) {
	if fsys == nil {
		return State{}, errors.New("updatecheck: nil filesystem")
	}
	fps, err := KeyFingerprints(opts)
	if err != nil {
		return State{}, err
	}
	st, exists, err := LoadState(fsys, path)
	if err != nil {
		return State{}, err
	}
	if !exists {
		st = State{SchemaVersion: StateSchemaVersion, extra: map[string]json.RawMessage{}}
	}
	st.SchemaVersion = StateSchemaVersion
	if st.KeyFingerprints == nil {
		st.KeyFingerprints = map[string]string{}
	}
	for name, value := range fps {
		st.KeyFingerprints[name] = value
	}
	st.RecordedAt = now.UTC().Format(time.RFC3339)
	if err := SaveState(fsys, path, st); err != nil {
		return State{}, err
	}
	return st, nil
}
