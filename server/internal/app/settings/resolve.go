// Package settings 内的覆盖链解析（sett2 批）。
//
// # 功能
// Resolver 实现「settings 表 DB 覆盖 > env > registry 缺省」的三层读链：
// DB 行存在且合法 → 行值；行缺失 → 调用方传入的 env/registry 值；
// 行存在但非法（手工 SQL 写坏）→ 回退 env 值并每键出声一次（fail-open 但留痕）。
// 名册（Roster）经 Lookup 报告 source=db 的覆盖读数；
// 写面（SetSchedule）经 CheckInt 复用同一份 registry 域。
//
// # 归属
// app 层用例。覆盖行读取端口由 adapters/pg.SettingsRepo 实现；
// 数值域来自编译期嵌入名册（registry domain），由组合根注入，
// 本包**不**读环境变量、**不**写 SQL、**不**解析名册文件。
//
// # 口径
// 存储故障原样上抛，**不**当作缺行：故障不是配置状态。
// 非法 DB 值按「坏行」处理：回退 env 并 Warn 一次（只记键名与类型分类，
// 不记值原文——坏值可能来自任何人手工写入，不进日志）。
// 值域只认名册 domain；写面与读面共用同一份，不复制第二份 [0,300]。
package settings

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"

	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
)

// 可写回的调度设置键（settings 表行名，批 sett2）。
// 三键之外不开写面：写面必须咬既有消费点（规则 7）。
const (
	// KeyScheduleEdgeFrontSec 是窗口前裁秒数（整型，域 [0,300]）。
	KeyScheduleEdgeFrontSec = "schedule.edge_front_sec"
	// KeyScheduleEdgeBackSec 是窗口后裁秒数（整型，域 [0,300]）。
	KeyScheduleEdgeBackSec = "schedule.edge_back_sec"
	// KeyScheduleAllowTimePref 是用户自选时间片总开关（布尔字面）。
	KeyScheduleAllowTimePref = "schedule.allow_time_pref"
)

// KeyEnginePaused 是全局执行暂停开关（批 3a 机制键）。
// DB-only 机制键：不经名册（registry 无对应键，Known 恒 false，
// 名册不会因它产生覆盖行查询）、无 env 对应位；
// 缺行=false=不暂停，与 auto_approve 口径一致。
// 读经覆盖链解析器（故障上抛、坏行回退缺省并出声），写经 SetBool。
const KeyEnginePaused = "engine.paused"

// 批 3b 的四枚 DB-only 机制键。与 engine.paused 同口径：不经名册
// （registry 无对应键，Known 恒 false，覆盖行不进 GET /api/admin/settings）、
// 无 env 对应位、三份环境模板不动。
const (
	// KeyScheduleSundaySign 是周日计划构建开关（布尔）。
	// 缺行=false=周日不铺任何计划行（对齐旧线 SUNDAY_SIGN 缺省 0）。
	KeyScheduleSundaySign = "schedule.sunday_sign"
	// KeyScheduleSaturdaySign 是周六计划构建开关（布尔）。
	// 缺行=false=周六不铺任何计划行（对齐旧线 SATURDAY_SIGN 缺省 0）。
	KeyScheduleSaturdaySign = "schedule.saturday_sign"
	// KeyScheduleDefaultSignStartMin 是全局默认签到窗口起点（业务日内分钟）。
	KeyScheduleDefaultSignStartMin = "schedule.default_sign_start_min"
	// KeyScheduleDefaultSignEndMin 是全局默认签到窗口终点（业务日内分钟）。
	KeyScheduleDefaultSignEndMin = "schedule.default_sign_end_min"
)

// 默认窗口分钟域 [0,1439]（业务日内分钟，00:00 起）。
// DB-only 键不经 registry：域没有名册载体，按批 3b 派单在用例层显式写死；
// 读面（ResolveSignWindow）与写面（SetSchedule 的域校验）共用此一对常量，
// 不复制第二份。上界与 core/schedule.MaxOffsetMin（1440，不含）同源：
// 业务日最多 1440 分钟，1439 是最后一个合法分钟。
const (
	signWindowMinuteMin = 0
	signWindowMinuteMax = 1439
)

// SettingDomain 是一个整型键的合法域（registry domain 注入视图）。
type SettingDomain struct {
	Min int
	Max int
}

// scheduleIntKeys 是整型写回键与其域名册域的键位说明。
// resolver 要求两枚 edge 键都有域；缺域即构造失败：写面不得猜域。
var scheduleIntKeys = []string{KeyScheduleEdgeFrontSec, KeyScheduleEdgeBackSec}

// registryAlias 把名册键（YIBAN_*，设置页展示用）映射到 settings 行键
// （读链 DB 覆盖的实际存储位）。设置页名册与覆盖行是同一配置概念的两层，
// 名册读数经此映射落到覆盖行；未登记的名册键不进覆盖查询。
var registryAlias = map[string]string{
	"YIBAN_WINDOW_EDGE_FRONT_SEC": KeyScheduleEdgeFrontSec,
	"YIBAN_WINDOW_EDGE_BACK_SEC":  KeyScheduleEdgeBackSec,
	"YIBAN_ALLOW_TIME_PREF":       KeyScheduleAllowTimePref,
}

// settingsKeyOf 返回键对应的 settings 行键：名册键走别名，
// 其余原样返回（消费点直接传行键）。
func settingsKeyOf(key string) string {
	if alias, ok := registryAlias[key]; ok {
		return alias
	}
	return key
}

// RegistryKeyAliases 返回名册键→settings 行键的映射副本（装配层用）。
// 组合根据此把名册 domain 注入到正确的行键上；映射本体只在此一处。
func RegistryKeyAliases() map[string]string {
	out := make(map[string]string, len(registryAlias))
	for registryKey, settingsKey := range registryAlias {
		out[registryKey] = settingsKey
	}
	return out
}

// isIntKey 报告 settings 行键是否整型写回键。
func isIntKey(key string) bool {
	for _, intKey := range scheduleIntKeys {
		if intKey == key {
			return true
		}
	}
	return false
}

// OverrideReader 是覆盖行的存在性感知读取端口。实现是 adapters/pg.SettingsRepo。
type OverrideReader interface {
	// IntOverride 读整型行：found=false 是缺行；
	// 行存在但值不是十进制整数报校验错。
	IntOverride(ctx context.Context, key string) (int, bool, error)
	// BoolOverride 读布尔行：只认小写 true/false 字面；坏行报校验错。
	BoolOverride(ctx context.Context, key string) (bool, bool, error)
}

// OverrideWriter 是覆盖行的写面端口。
// 调用方必须在事务内调用，使写入与审计同存亡。实现同上。
type OverrideWriter interface {
	SetInt(ctx context.Context, key string, value int, by string) error
	SetBool(ctx context.Context, key string, value bool, by string) error
	// ClearSetting 删行（PUT 显式 null＝回退 env）；删不存在的行是幂等成功。
	ClearSetting(ctx context.Context, key string) error
}

// ScheduleStore 是调度三键的完整存取端口（读＋写）。
type ScheduleStore interface {
	OverrideReader
	OverrideWriter
}

// Resolver 是 DB 覆盖链解析器。零值不可用。
type Resolver struct {
	store   OverrideReader
	domains map[string]SettingDomain
	log     *slog.Logger

	mu     sync.Mutex
	warned map[string]bool
}

// NewResolver 构造解析器。仓储缺失或整型键缺域即失败：
// 覆盖链没有「猜域」分支。
func NewResolver(store OverrideReader, domains map[string]SettingDomain, log *slog.Logger) (*Resolver, error) {
	if store == nil {
		return nil, errors.New("settings: nil override store")
	}
	if len(domains) == 0 {
		return nil, errors.New("settings: no override domains")
	}
	for _, key := range scheduleIntKeys {
		if _, ok := domains[key]; !ok {
			return nil, fmt.Errorf("settings: override key %s has no registry domain", key)
		}
	}
	if log == nil {
		log = slog.Default()
	}
	return &Resolver{
		store:   store,
		domains: domains,
		log:     log,
		warned:  make(map[string]bool),
	}, nil
}

// Known 报告键是否在写回键集内（接受名册键或 settings 行键）。
// 名册只对已知键做覆盖查询，其余 registry 键保持 env/default 读法，
// 不产生额外 DB 查询。
func (r *Resolver) Known(key string) bool {
	settingsKey := settingsKeyOf(key)
	if settingsKey == KeyScheduleAllowTimePref {
		return true
	}
	_, ok := r.domains[settingsKey]
	return ok
}

// CheckInt 校验整型写回值在名册域内。写面（SetSchedule）与读面共用同一份域。
func (r *Resolver) CheckInt(key string, value int) error {
	domain, ok := r.domains[key]
	if !ok {
		return coreerrs.New(coreerrs.CodeValidation,
			fmt.Sprintf("setting %s is not writable as an integer", key))
	}
	if value < domain.Min || value > domain.Max {
		return coreerrs.New(coreerrs.CodeValidation,
			fmt.Sprintf("setting %s must be between %d and %d", key, domain.Min, domain.Max))
	}
	return nil
}

// isSettingValueInvalid 判定覆盖行读取错误是否为「坏行」：
// 存储侧对值不符合类型口径一律报校验错；连接等故障不是校验错。
func isSettingValueInvalid(err error) bool {
	e := coreerrs.From(err)
	return e != nil && e.Code == coreerrs.CodeValidation
}

// ResolveInt 返回生效的整型值：DB 行存在且在域内→行值；
// 行缺失→envValue；坏行→每键 Warn 一次后回退 envValue；
// 存储故障→原样上抛。
func (r *Resolver) ResolveInt(ctx context.Context, key string, envValue int) (int, error) {
	value, found, err := r.store.IntOverride(ctx, key)
	if err != nil {
		if isSettingValueInvalid(err) {
			r.warnOnce(key, "int")
			return envValue, nil
		}
		return 0, err
	}
	if !found {
		return envValue, nil
	}
	if err := r.CheckInt(key, value); err != nil {
		r.warnOnce(key, "int")
		return envValue, nil
	}
	return value, nil
}

// ResolveBool 返回生效的布尔值：DB 行存在且是真字面→行值；
// 行缺失或坏行→envValue（坏行先 Warn 一次）；存储故障→原样上抛。
func (r *Resolver) ResolveBool(ctx context.Context, key string, envValue bool) (bool, error) {
	value, found, err := r.store.BoolOverride(ctx, key)
	if err != nil {
		if isSettingValueInvalid(err) {
			r.warnOnce(key, "bool")
			return envValue, nil
		}
		return false, err
	}
	if !found {
		return envValue, nil
	}
	return value, nil
}

// Lookup 是名册视图的覆盖读数（接受名册键或 settings 行键）：
// found=true 时 value 是 int 或 bool。坏行按未覆盖处理（found=false），
// 但保留 Warn 一次的口径。存储故障原样上抛。
func (r *Resolver) Lookup(ctx context.Context, key string) (found bool, value any, err error) {
	settingsKey := settingsKeyOf(key)
	switch {
	case settingsKey == KeyScheduleAllowTimePref:
		boolValue, boolFound, boolErr := r.store.BoolOverride(ctx, settingsKey)
		if boolErr != nil {
			if isSettingValueInvalid(boolErr) {
				r.warnOnce(settingsKey, "bool")
				return false, nil, nil
			}
			return false, nil, boolErr
		}
		return boolFound, boolValue, nil
	case isIntKey(settingsKey):
		intValue, intFound, intErr := r.store.IntOverride(ctx, settingsKey)
		if intErr != nil {
			if isSettingValueInvalid(intErr) {
				r.warnOnce(settingsKey, "int")
				return false, nil, nil
			}
			return false, nil, intErr
		}
		if !intFound {
			return false, nil, nil
		}
		if err := r.CheckInt(settingsKey, intValue); err != nil {
			r.warnOnce(settingsKey, "int")
			return false, nil, nil
		}
		return true, intValue, nil
	default:
		return false, nil, nil
	}
}

// warnOnce 是坏值的出声口径：每键每进程只 Warn 一次。
// 只记键名与类型分类，不记值原文。
func (r *Resolver) warnOnce(key, kind string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.warned[key] {
		return
	}
	r.warned[key] = true
	r.log.Warn("settings override value is invalid; falling back to environment",
		logfield.KeyEvent, "settings.override_invalid",
		logfield.KeyKind, kind,
		logfield.KeyReason, key,
	)
}

// warnSignWindowPairOnce 是默认窗口对的出声口径：每进程只 Warn 一次。
// reason 是回退原因词（bad_row/half_pair/out_of_domain/start_not_before_end），
// 不含值原文。警告以 start 键为索引（对级问题共用一条记录）。
func (r *Resolver) warnSignWindowPairOnce(reason string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.warned[KeyScheduleDefaultSignStartMin] {
		return
	}
	r.warned[KeyScheduleDefaultSignStartMin] = true
	r.log.Warn("schedule default sign window override is invalid; falling back to built-in constants",
		logfield.KeyEvent, "settings.override_invalid",
		logfield.KeyKind, "pair",
		logfield.KeyReason, reason,
	)
}

// ResolveSignWindow 解析全局默认签到窗口对（批 3b，消费点＝fallback 窗口）。
// 两侧行都存在且都在 [0,1439] 且 start<end → 行值对；
// 双缺行 → 常量对（调用方传 core/schedule 缺省常量，常量仍单源在 core）；
// 半对（一侧有行一侧缺行）、坏行、越域或 start>=end → 整对回退常量并
// 每进程 Warn 一次——半对配置不得使用，防止跨语义拼接；
// 存储故障原样上抛（故障不是配置状态）。
func (r *Resolver) ResolveSignWindow(ctx context.Context, fallbackStart, fallbackEnd int) (int, int, error) {
	start, startFound, err := r.store.IntOverride(ctx, KeyScheduleDefaultSignStartMin)
	if err != nil {
		if isSettingValueInvalid(err) {
			r.warnSignWindowPairOnce("bad_row")
			return fallbackStart, fallbackEnd, nil
		}
		return 0, 0, err
	}
	end, endFound, err := r.store.IntOverride(ctx, KeyScheduleDefaultSignEndMin)
	if err != nil {
		if isSettingValueInvalid(err) {
			r.warnSignWindowPairOnce("bad_row")
			return fallbackStart, fallbackEnd, nil
		}
		return 0, 0, err
	}
	switch {
	case !startFound && !endFound:
		// 双缺行是常态：静默用常量对（对齐覆盖链「缺行→传参缺省」口径）。
		return fallbackStart, fallbackEnd, nil
	case startFound != endFound:
		// 半对：一侧有行一侧缺行。整对回退，不与常量拼接另一侧行值。
		r.warnSignWindowPairOnce("half_pair")
		return fallbackStart, fallbackEnd, nil
	}
	if start < signWindowMinuteMin || start > signWindowMinuteMax ||
		end < signWindowMinuteMin || end > signWindowMinuteMax {
		r.warnSignWindowPairOnce("out_of_domain")
		return fallbackStart, fallbackEnd, nil
	}
	if start >= end {
		r.warnSignWindowPairOnce("start_not_before_end")
		return fallbackStart, fallbackEnd, nil
	}
	return start, end, nil
}
