// Package settings 内的 SMTP 设置读写与每发送解析（批 smtp4）。
//
// # 功能
// Service.MailSettings 回显 mail 六键的生效值与来源（db/env/default）；
// Service.SetMail 部分补丁写回六键：值=upsert、null=删行回 env；
// MailResolver.Resolve 供 GET 与每次 SMTP 外呼共用同一条读链。
// mail.pass 以 account cipher 的 AES-GCM 密文（base64）落 settings 行，
// 明文只在内存中流转：不进日志、审计、响应或错误文本。
//
// # 归属
// app 层用例。覆盖行存取端口见 resolve.go，adapters/pg 实现；
// 密文由 app/account.Cipher 的 SealSecret/OpenSecret 提供（同一把钥）；
// 入口层只做角色门、二次口令与传输映射。
//
// # 口径
// 读链与 sett2/3b/3c/r4r 同形：有效 DB 行 > env > registry 缺省；
// 坏行（空主机、越域端口、词表外 TLS 档、无法解密的密文）回退 env
// 并每键 Warn 一次，坏值原文绝不进日志。
// 写面只有 master；域校验在事务外完成，任何拒绝零写入零审计。
// 敏感键（from/user/pass）的审计只记 set/cleared 状态词
// （unchanged 保留给未来幂等判定，本批不产出），
// 非敏感键（host/port/tls_mode）记字面旧→新值。
// 写入与恰一行 settings.mail_writeback 审计同事务；审计失败整体回滚。
// 每发送解析：下一次外呼即用新值，无需重启。
package settings

import (
	"context"
	"log/slog"
	"net"
	"net/mail"
	"slices"
	"strconv"
	"strings"
	"sync"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// SMTP 六键的 settings 行名（DB-only，不经 registry 名册）。
// 缺行=回 env/registry 值；写面只认这六键。
const (
	// KeyMailSMTPHost 是 SMTP 服务器主机名（非敏感，回显字面值）。
	KeyMailSMTPHost = "mail.smtp_host"
	// KeyMailSMTPPort 是 SMTP 端口（非敏感，十进制字符串存储）。
	KeyMailSMTPPort = "mail.smtp_port"
	// KeyMailTLSMode 是 TLS 模式（非敏感，core/mail 词表）。
	KeyMailTLSMode = "mail.tls_mode"
	// KeyMailFrom 是发件地址（展示敏感：只回掩码，审计只记状态词）。
	KeyMailFrom = "mail.from"
	// KeyMailUser 是 AUTH PLAIN 账号（展示敏感：只回掩码，审计只记状态词）。
	KeyMailUser = "mail.user"
	// KeyMailPass 是 AUTH PLAIN 口令（密文落库：只回 set，审计只记状态词）。
	KeyMailPass = "mail.pass"
)

// mailPassDomain 是 mail.pass 密文的 AAD 域标签。
// 与凭据的 school|account AAD 隔离：同一把钥，不同用途互不可解。
const mailPassDomain = "yiban.mail.pass.v1"

// MailSource 是 mail 键生效值的来源词表。
// 与配额键不同，mail 六键都有 env/registry 对应位，故有 env 与 default 两档
// 非 DB 来源；契约枚举 MailSettingSource 与本词表同集（enum_gate 钉住）。
type MailSource string

const (
	// MailSourceDB 表示有效覆盖行提供该键。
	MailSourceDB MailSource = "db"
	// MailSourceEnv 表示进程环境提供该键。
	MailSourceEnv MailSource = "env"
	// MailSourceDefault 表示环境未提供，读 registry 缺省。
	MailSourceDefault MailSource = "default"
)

// MailEnvValue 是一个键的 env/registry 兜底值与来源。
// 来源由装配端按「env 是否显式给非空值」判定，本包不读环境变量。
type MailEnvValue struct {
	Value  string
	Source MailSource
}

// MailEnv 是 mail 六键的 env/registry 兜底值。
// Port 用十进制字符串承载，与覆盖行的存储形态一致。
type MailEnv struct {
	Host    MailEnvValue
	Port    MailEnvValue
	TLSMode MailEnvValue
	From    MailEnvValue
	User    MailEnvValue
	Pass    MailEnvValue
}

// MailValue 是非敏感键的对外视图：真实值＋来源。
type MailValue struct {
	Value  string
	Source MailSource
}

// MailPortValue 是端口键的对外视图：整数＋来源。
type MailPortValue struct {
	Value  int
	Source MailSource
}

// MailSensitiveValue 是展示敏感键（from/user）的对外视图：
// 只给 set 与固定掩码，永不回显原文。
type MailSensitiveValue struct {
	Set    bool
	Masked string
	Source MailSource
}

// MailSecretValue 是口令键的对外视图：只给 set 与来源，**无掩码字段**。
type MailSecretValue struct {
	Set    bool
	Source MailSource
}

// MailReport 是 mail 六键的只读视图。
type MailReport struct {
	Host    MailValue
	Port    MailPortValue
	TLSMode MailValue
	From    MailSensitiveValue
	User    MailSensitiveValue
	Pass    MailSecretValue
}

// MailConfig 是每次 SMTP 外呼使用的生效配置。
// Pass 是明文，只在内存中流转；调用方不得记录或回显。
type MailConfig struct {
	Host    string
	Port    int
	TLSMode string
	From    string
	User    string
	Pass    string
}

// SecretCipher 是 mail.pass 的密文端口。
// 生产实现是 app/account.Cipher（同一把 YIBAN_CREDENTIAL_KEY）；
// 测试注入失败替身以钉住「加密失败 500 不写库」。
type SecretCipher interface {
	SealSecret(domain, plaintext string) (string, error)
	OpenSecret(domain, blob string) (string, error)
}

// mailKeySpec 是一个 mail 键的读校验分类。
type mailKeySpec struct {
	key  string
	kind string
}

// mailKeySpecs 是六键的固定顺序：读链与写面共用，不复制第二份键集。
var mailKeySpecs = []mailKeySpec{
	{KeyMailSMTPHost, "host"},
	{KeyMailSMTPPort, "port"},
	{KeyMailTLSMode, "tls_mode"},
	{KeyMailFrom, "from"},
	{KeyMailUser, "user"},
	{KeyMailPass, "secret"},
}

// mailKeyKind 返回键的读校验分类；未知键返回空串。
func mailKeyKind(key string) string {
	for _, spec := range mailKeySpecs {
		if spec.key == key {
			return spec.kind
		}
	}
	return ""
}

// MailResolver 解析 mail 六键的生效值（DB>env>registry）。
// 读面（GET）与每发送解析共用同一实例：坏行 Warn 一次的口径只有一份。
type MailResolver struct {
	store  OverrideReader
	cipher SecretCipher
	env    MailEnv
	log    *slog.Logger

	mu     sync.Mutex
	warned map[string]bool
}

// NewMailResolver 构造 mail 覆盖链解析器。
// store/cipher 为 nil 时 Resolve 失败关闭（CodeInternal），不在构造期 panic：
// 装配缺失必须显性，不得静默读成空配置。
func NewMailResolver(store OverrideReader, cipher SecretCipher, env MailEnv, log *slog.Logger) *MailResolver {
	if log == nil {
		log = slog.Default()
	}
	return &MailResolver{
		store:  store,
		cipher: cipher,
		env:    env,
		log:    log,
		warned: make(map[string]bool),
	}
}

// Resolve 返回六键的对外视图与生效配置。
// 存储故障原样上抛；坏行回退 env 并每键 Warn 一次；
// 密文无法解密按坏行处理（回退 env），绝不把坏值当空口令。
func (r *MailResolver) Resolve(ctx context.Context) (MailReport, MailConfig, error) {
	if r.store == nil {
		return MailReport{}, MailConfig{}, coreerrs.New(coreerrs.CodeInternal,
			"mail settings store is not configured")
	}
	if r.cipher == nil {
		return MailReport{}, MailConfig{}, coreerrs.New(coreerrs.CodeInternal,
			"mail password cipher is not configured")
	}
	values := make(map[string]string, len(mailKeySpecs))
	sources := make(map[string]MailSource, len(mailKeySpecs))
	for _, spec := range mailKeySpecs {
		raw, found, valid, err := readMailDBValue(ctx, r.store, r.cipher, spec.key)
		if err != nil {
			return MailReport{}, MailConfig{}, err
		}
		if found && valid {
			values[spec.key] = raw
			sources[spec.key] = MailSourceDB
			continue
		}
		if found {
			r.warnOnce(spec.key, spec.kind)
		}
		env := r.envValue(spec.key)
		values[spec.key] = env.Value
		sources[spec.key] = env.Source
	}
	port, ok := parseMailPort(values[KeyMailSMTPPort])
	if !ok {
		// env 端口由 config.Load 校验；走到这里说明装配注入了坏值。
		return MailReport{}, MailConfig{}, coreerrs.New(coreerrs.CodeInternal,
			"mail settings environment port is invalid")
	}
	report := MailReport{
		Host:    MailValue{Value: values[KeyMailSMTPHost], Source: sources[KeyMailSMTPHost]},
		Port:    MailPortValue{Value: port, Source: sources[KeyMailSMTPPort]},
		TLSMode: MailValue{Value: values[KeyMailTLSMode], Source: sources[KeyMailTLSMode]},
		From:    mailSensitiveValue(values[KeyMailFrom], sources[KeyMailFrom]),
		User:    mailSensitiveValue(values[KeyMailUser], sources[KeyMailUser]),
		Pass: MailSecretValue{
			Set:    values[KeyMailPass] != "",
			Source: sources[KeyMailPass],
		},
	}
	config := MailConfig{
		Host:    values[KeyMailSMTPHost],
		Port:    port,
		TLSMode: values[KeyMailTLSMode],
		From:    values[KeyMailFrom],
		User:    values[KeyMailUser],
		Pass:    values[KeyMailPass],
	}
	return report, config, nil
}

// mailSensitiveValue 构造展示敏感键视图：set 由有效值非空决定，
// 掩码是固定 "****"，不含原值任何字符。
func mailSensitiveValue(value string, source MailSource) MailSensitiveValue {
	set := value != ""
	masked := ""
	if set {
		masked = maskedValue
	}
	return MailSensitiveValue{Set: set, Masked: masked, Source: source}
}

// envValue 返回键的 env/registry 兜底值；来源缺省按 default。
func (r *MailResolver) envValue(key string) MailEnvValue {
	var value MailEnvValue
	switch key {
	case KeyMailSMTPHost:
		value = r.env.Host
	case KeyMailSMTPPort:
		value = r.env.Port
	case KeyMailTLSMode:
		value = r.env.TLSMode
	case KeyMailFrom:
		value = r.env.From
	case KeyMailUser:
		value = r.env.User
	case KeyMailPass:
		value = r.env.Pass
	}
	if value.Source == "" {
		value.Source = MailSourceDefault
	}
	return value
}

// warnOnce 是坏行的出声口径：每键每进程只 Warn 一次。
// 只记键名与类型分类，不记值原文——坏值可能来自任何人手工写入。
func (r *MailResolver) warnOnce(key, kind string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.warned[key] {
		return
	}
	r.warned[key] = true
	r.log.Warn("mail settings override is invalid; falling back to environment",
		logfield.KeyEvent, "settings.override_invalid",
		logfield.KeyKind, kind,
		logfield.KeyReason, key,
	)
}

// readMailDBValue 读一个 mail 键的覆盖行并校验。
// found=false 是缺行；valid=false 是坏行（值形状错或密文无法解密）。
// 返回的 raw 对 pass 是解密后的明文，其余是裁剪后的存储值。
// 坏行错误不携带值原文。
func readMailDBValue(ctx context.Context, store OverrideReader, cipher SecretCipher,
	key string) (raw string, found, valid bool, err error) {
	stored, found, err := store.StringOverride(ctx, key)
	if err != nil || !found {
		return "", found, false, err
	}
	switch kind := mailKeyKind(key); kind {
	case "host":
		host, ok := normalizeSMTPHost(stored)
		if !ok {
			return "", true, false, nil
		}
		return host, true, true, nil
	case "port":
		if _, ok := parseMailPort(stored); !ok {
			return "", true, false, nil
		}
		return strings.TrimSpace(stored), true, true, nil
	case "tls_mode":
		if !validMailTLSMode(stored) {
			return "", true, false, nil
		}
		return strings.TrimSpace(stored), true, true, nil
	case "from":
		if !validMailFrom(stored) {
			return "", true, false, nil
		}
		return strings.TrimSpace(stored), true, true, nil
	case "user":
		if !validMailUser(stored) {
			return "", true, false, nil
		}
		return strings.TrimSpace(stored), true, true, nil
	case "secret":
		if cipher == nil {
			return "", true, false, coreerrs.New(coreerrs.CodeInternal,
				"mail password cipher is not configured")
		}
		plain, openErr := cipher.OpenSecret(mailPassDomain, stored)
		if openErr != nil || plain == "" {
			return "", true, false, nil
		}
		return plain, true, true, nil
	default:
		return "", true, false, coreerrs.New(coreerrs.CodeInternal,
			"unknown mail settings key")
	}
}

// MailChange 是一个 mail 键的写意图。
// Op=set 时 Value 是 string（from/user/pass/host/tls_mode）或 int（port）；
// Op=clear 时 Value 必须为 nil。
type MailChange struct {
	Key   string
	Op    WriteOp
	Value any
}

// MailSettings 读 mail 六键的生效值与来源。master 与 admin 可读。
// 存储故障原样上抛，**不**当作缺省：故障不是配置状态。
func (s *Service) MailSettings(ctx context.Context, scope corescope.Scope) (MailReport, error) {
	if err := app.RequireScope(scope); err != nil {
		return MailReport{}, err
	}
	if kind := scope.Kind(); kind != corescope.KindMaster && kind != corescope.KindAdmin {
		return MailReport{}, coreerrs.New(coreerrs.CodeForbidden, "master or admin role is required")
	}
	if s.deps.MailResolver == nil {
		return MailReport{}, coreerrs.New(coreerrs.CodeInternal,
			"mail settings resolver is not configured")
	}
	report, _, err := s.deps.MailResolver.Resolve(ctx)
	return report, err
}

// preparedMailChange 是校验与加密后的写意图。
// storage 是落 settings 行的值（pass 是密文，port 是十进制串）；
// audit 是审计 detail 的新态（敏感键 set/cleared，非敏感键字面值）。
type preparedMailChange struct {
	key       string
	op        WriteOp
	storage   string
	audit     any
	sensitive bool
}

// SetMail 部分补丁写回 mail 六键。**只有 master 可以调用**。
// 校验与加密都在事务外完成：任何拒绝零写入零审计。
// 写入与 settings.mail_writeback 审计同事务；审计失败整体回滚。
func (s *Service) SetMail(ctx context.Context, scope corescope.Scope, actor appaudit.Actor,
	changes []MailChange) error {
	if err := app.RequireScope(scope); err != nil {
		return err
	}
	if scope.Kind() != corescope.KindMaster {
		return coreerrs.New(coreerrs.CodeForbidden, "master role is required")
	}
	if s.deps.Schedule == nil {
		return coreerrs.New(coreerrs.CodeInternal, "mail settings store is not configured")
	}
	if s.deps.Cipher == nil {
		return coreerrs.New(coreerrs.CodeInternal, "mail password cipher is not configured")
	}
	if len(changes) == 0 {
		return coreerrs.New(coreerrs.CodeValidation, "mail settings update is empty")
	}
	prepared, err := prepareMailChanges(changes, s.deps.Cipher)
	if err != nil {
		return err
	}
	return s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		pairs := make(map[string][2]any, len(prepared))
		for _, change := range prepared {
			old, err := s.readMailOld(ctx, change)
			if err != nil {
				return err
			}
			if change.op == OpClear {
				if err := s.deps.Schedule.ClearSetting(ctx, change.key); err != nil {
					return err
				}
			} else if err := s.deps.Schedule.SetString(ctx, change.key, change.storage, actor.UserID); err != nil {
				return err
			}
			pairs[change.key] = [2]any{old, change.audit}
		}
		_, err := s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionSettingsMailWriteback,
			TargetType:  "settings",
			TargetID:    "mail",
			Detail:      map[string]any{"changes": pairs},
		})
		return err
	})
}

// prepareMailChanges 在事务外做全部拒绝判定与口令加密。
// 口令先加密再解密回验：加密失败或回验失败一律 CodeInternal，零写入。
func prepareMailChanges(changes []MailChange, cipher SecretCipher) ([]preparedMailChange, error) {
	out := make([]preparedMailChange, 0, len(changes))
	for _, change := range changes {
		kind := mailKeyKind(change.Key)
		if kind == "" {
			return nil, coreerrs.New(coreerrs.CodeValidation, "unknown mail setting key")
		}
		if change.Op != OpSet && change.Op != OpClear {
			return nil, coreerrs.New(coreerrs.CodeValidation, "unknown mail settings operation")
		}
		if change.Op == OpClear {
			if change.Value != nil {
				return nil, coreerrs.New(coreerrs.CodeValidation,
					"clearing a setting must not carry a value")
			}
			var audit any
			if mailKeySensitive(change.Key) {
				audit = mailAuditState(false)
			}
			out = append(out, preparedMailChange{
				key:       change.Key,
				op:        OpClear,
				audit:     audit,
				sensitive: mailKeySensitive(change.Key),
			})
			continue
		}
		storage, audit, err := prepareMailSet(change.Key, kind, change.Value, cipher)
		if err != nil {
			return nil, err
		}
		out = append(out, preparedMailChange{
			key:       change.Key,
			op:        OpSet,
			storage:   storage,
			audit:     audit,
			sensitive: mailKeySensitive(change.Key),
		})
	}
	return out, nil
}

// prepareMailSet 校验一个 set 值并返回落库值与审计新态。
func prepareMailSet(key, kind string, value any, cipher SecretCipher) (string, any, error) {
	switch kind {
	case "host":
		host, ok := value.(string)
		if !ok {
			return "", nil, coreerrs.New(coreerrs.CodeValidation, "mail host requires a string")
		}
		normalized, ok := normalizeSMTPHost(host)
		if !ok {
			return "", nil, coreerrs.New(coreerrs.CodeValidation, "mail host is not a valid host name")
		}
		// 落库与审计都用归一形态：raw 254 的尾点主机不得在审计形状处 500。
		return normalized, normalized, nil
	case "port":
		port, ok := value.(int)
		if !ok {
			return "", nil, coreerrs.New(coreerrs.CodeValidation, "mail port requires an integer")
		}
		if port < 1 || port > 65535 {
			return "", nil, coreerrs.New(coreerrs.CodeValidation,
				"mail port must be between 1 and 65535")
		}
		return strconv.Itoa(port), port, nil
	case "tls_mode":
		mode, ok := value.(string)
		if !ok {
			return "", nil, coreerrs.New(coreerrs.CodeValidation, "mail tls_mode requires a string")
		}
		if !validMailTLSMode(mode) {
			return "", nil, coreerrs.New(coreerrs.CodeValidation,
				"mail tls_mode must be auto, implicit or starttls")
		}
		return mode, mode, nil
	case "from":
		from, ok := value.(string)
		if !ok {
			return "", nil, coreerrs.New(coreerrs.CodeValidation, "mail from requires a string")
		}
		from = strings.TrimSpace(from)
		if !validMailFrom(from) {
			return "", nil, coreerrs.New(coreerrs.CodeValidation, "mail from is not a valid address")
		}
		return from, "set", nil
	case "user":
		user, ok := value.(string)
		if !ok {
			return "", nil, coreerrs.New(coreerrs.CodeValidation, "mail user requires a string")
		}
		user = strings.TrimSpace(user)
		if !validMailUser(user) {
			return "", nil, coreerrs.New(coreerrs.CodeValidation, "mail user must not be empty")
		}
		return user, "set", nil
	case "secret":
		pass, ok := value.(string)
		if !ok {
			return "", nil, coreerrs.New(coreerrs.CodeValidation, "mail pass requires a string")
		}
		if pass == "" {
			return "", nil, coreerrs.New(coreerrs.CodeValidation, "mail pass must not be empty")
		}
		if cipher == nil {
			return "", nil, coreerrs.New(coreerrs.CodeInternal, "mail password cipher is not configured")
		}
		blob, err := cipher.SealSecret(mailPassDomain, pass)
		if err != nil {
			return "", nil, coreerrs.New(coreerrs.CodeInternal, "mail password cannot be encrypted")
		}
		// 回验：加密结果必须能被同一把钥解回同一口令。
		// 失败是加密器异常态，不是用户输入错误；500 且不写库。
		roundTrip, err := cipher.OpenSecret(mailPassDomain, blob)
		if err != nil || roundTrip != pass {
			return "", nil, coreerrs.New(coreerrs.CodeInternal, "mail password cannot be encrypted")
		}
		return blob, "set", nil
	default:
		return "", nil, coreerrs.New(coreerrs.CodeValidation, "unknown mail setting key")
	}
}

// mailKeySensitive 报告键是否按敏感位审计（只记状态词）。
func mailKeySensitive(key string) bool {
	return key == KeyMailFrom || key == KeyMailUser || key == KeyMailPass
}

// mailAuditState 返回敏感键的审计状态词。
// set=true 表示本次写入后该键在覆盖行里有值；false 表示删行。
func mailAuditState(set bool) string {
	if set {
		return "set"
	}
	return "cleared"
}

// readMailOld 读一个键的覆盖行旧态作审计标量。
// 非敏感键返回字面旧值（坏行/缺行记 nil）；敏感键只返回 set/cleared，
// 原文与密文都不进审计。存储故障原样上抛。
func (s *Service) readMailOld(ctx context.Context, change preparedMailChange) (any, error) {
	if change.sensitive {
		_, found, valid, err := readMailDBValue(ctx, s.deps.Schedule, s.deps.Cipher, change.key)
		if err != nil {
			return nil, err
		}
		return mailAuditState(found && valid), nil
	}
	raw, found, valid, err := readMailDBValue(ctx, s.deps.Schedule, s.deps.Cipher, change.key)
	if err != nil {
		return nil, err
	}
	if !found || !valid {
		return nil, nil
	}
	if change.key == KeyMailSMTPPort {
		port, ok := parseMailPort(raw)
		if !ok {
			return nil, nil
		}
		return port, nil
	}
	return raw, nil
}

// normalizeSMTPHost 归一主机名并校验合法性。
// 去首尾空白与单个尾点后校验；返回的归一形态用于落库与审计，
// 避免「raw 254 过校验、归一 253 超审计形状」的 500 地雷。
// 拒绝空串、空白、scheme、路径与端口后缀：这些会让拨号目标与证书校验漂移。
func normalizeSMTPHost(raw string) (string, bool) {
	host := strings.TrimSuffix(strings.TrimSpace(raw), ".")
	if host == "" || len(host) > 253 {
		return "", false
	}
	if net.ParseIP(host) != nil {
		return host, true
	}
	for _, label := range strings.Split(host, ".") {
		if label == "" || len(label) > 63 {
			return "", false
		}
		if label[0] == '-' || label[len(label)-1] == '-' {
			return "", false
		}
		for _, r := range label {
			switch {
			case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-':
			default:
				return "", false
			}
		}
	}
	return host, true
}

// parseMailPort 解析端口十进制串并校验 [1,65535]。
func parseMailPort(raw string) (int, bool) {
	value, err := strconv.Atoi(strings.TrimSpace(raw))
	if err != nil || value < 1 || value > 65535 {
		return 0, false
	}
	return value, true
}

// validMailTLSMode 报告 TLS 档是否在 core/mail 词表内（只认小写字面）。
func validMailTLSMode(raw string) bool {
	return slices.Contains(coremail.TLSModes(), raw)
}

// validMailFrom 报告发件地址是否可被投递侧表达。
// 接受 net/mail 严格解析的地址（显示名会被投递侧丢弃）；
// 拒绝换行、控制字符与非 ASCII addr-spec——投递侧不发 SMTPUTF8。
func validMailFrom(raw string) bool {
	s := strings.TrimSpace(raw)
	if s == "" || strings.ContainsAny(s, "\r\n") {
		return false
	}
	for _, r := range s {
		if r < 0x20 || r == 0x7F {
			return false
		}
	}
	addr, err := mail.ParseAddress(s)
	if err != nil || addr.Address == "" {
		return false
	}
	for _, r := range addr.Address {
		if r >= 0x80 {
			return false
		}
	}
	return true
}

// validMailUser 报告 AUTH 账号是否可收：非空、无换行与控制字符。
// 账号不一定是邮箱（SMTP 登录名可以是任意串），故不做地址校验。
func validMailUser(raw string) bool {
	s := strings.TrimSpace(raw)
	if s == "" || strings.ContainsAny(s, "\r\n") {
		return false
	}
	for _, r := range s {
		if r < 0x20 || r == 0x7F {
			return false
		}
	}
	return true
}
