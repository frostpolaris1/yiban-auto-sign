// Package settings 实现系统设置只读名册与全站公告用例（2ws A）。
//
// # 功能
// Roster 把配置名册按组展开为只读报告：非敏感键给解析值，
// 敏感键只给 {set, masked, source}，永不回显原文。
// Service 读写全站公告：正文单行、去首尾空白、上限 200 字符；
// 写入与 settings.announcement 审计同事务。
// 本包**不**读环境变量、**不**读名册文件、**不**写 SQL。
//
// # 归属
// app 层用例。名册由 adapters/config 解析后注入；公告端口见本文件，
// adapters/pg 实现。入口层只做角色门与传输映射。
//
// # 复用
// Roster、Entry、Report、Group、Item；Service、Announcement、Store、Deps；
// MaxAnnouncementChars、Source 常量。
//
// # 口径
// source 只报告能跟踪到的事实：环境提供即 env，名册登记缺省即 default，
// 两者都没有即 unknown。file 保留给文件层，本服务永不产出。
// 敏感键的 masked 是固定 "****"，不含原值任何字符。
package settings

import (
	"context"
	"errors"
	"sort"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// Source 是配置值的来源分类。
type Source string

const (
	// SourceEnv 表示进程环境提供该键。
	SourceEnv Source = "env"
	// SourceFile 表示文件配置层提供该键；本服务永不产出。
	SourceFile Source = "file"
	// SourceDefault 表示环境未提供且名册登记了缺省值。
	SourceDefault Source = "default"
	// SourceUnknown 表示环境未提供且名册没有缺省值。
	SourceUnknown Source = "unknown"
)

// maskedValue 是敏感键的固定掩码。它不含原值任何字符。
const maskedValue = "****"

// ValueSource 读取配置值的最小环境面。实现是 adapters/config.Env。
type ValueSource interface {
	Lookup(key string) (string, bool)
}

// Entry 是名册里的一行。
// Default 为 nil 且 HasDefault 为假表示该键没有缺省值；
// 缺省值显式为 null 也按「无缺省」处理（名册口径）。
type Entry struct {
	Key        string
	Group      string
	Sensitive  bool
	Default    any
	HasDefault bool
}

// Sensitive 是敏感键的对外视图。
type Sensitive struct {
	Set    bool
	Masked string
	Source Source
}

// Item 是一个键的对外视图。
// 敏感键 Value 为 nil 且 Sensitive 非空；非敏感键相反。
type Item struct {
	Key       string
	Group     string
	Source    Source
	Value     any
	Sensitive *Sensitive
}

// Group 是一组设置。
type Group struct {
	Name  string
	Items []Item
}

// Report 是全部分组的只读视图。分组与组内条目都按键名排序，顺序稳定。
type Report struct {
	Groups []Group
}

// Roster 是只读配置名册。
type Roster struct {
	entries []Entry
	source  ValueSource
}

// NewRoster 构造名册报告器。名册为空、键重复或来源缺失即失败：
// 空名册不得当绿。
func NewRoster(entries []Entry, source ValueSource) (*Roster, error) {
	if source == nil {
		return nil, errors.New("settings: nil value source")
	}
	if len(entries) == 0 {
		return nil, errors.New("settings: empty configuration roster")
	}
	seen := make(map[string]bool, len(entries))
	copied := make([]Entry, 0, len(entries))
	for _, entry := range entries {
		if entry.Key == "" {
			return nil, errors.New("settings: roster key is required")
		}
		if seen[entry.Key] {
			return nil, errors.New("settings: duplicate roster key " + entry.Key)
		}
		seen[entry.Key] = true
		copied = append(copied, entry)
	}
	sort.Slice(copied, func(i, j int) bool { return copied[i].Key < copied[j].Key })
	return &Roster{entries: copied, source: source}, nil
}

// Report 展开全部键。敏感键只给掩码视图。
func (r *Roster) Report() Report {
	byGroup := map[string][]Item{}
	for _, entry := range r.entries {
		item := r.item(entry)
		byGroup[entry.Group] = append(byGroup[entry.Group], item)
	}
	names := make([]string, 0, len(byGroup))
	for name := range byGroup {
		names = append(names, name)
	}
	sort.Strings(names)
	groups := make([]Group, 0, len(names))
	for _, name := range names {
		groups = append(groups, Group{Name: name, Items: byGroup[name]})
	}
	return Report{Groups: groups}
}

// item 解析单个键的来源与值。环境值为空串按未设置处理：
// 与 config.Load 的取值口径一致，避免空值冒充已设置。
func (r *Roster) item(entry Entry) Item {
	raw, provided := r.source.Lookup(entry.Key)
	set := provided && raw != ""
	source := SourceUnknown
	switch {
	case set:
		source = SourceEnv
	case entry.HasDefault:
		source = SourceDefault
	}
	item := Item{Key: entry.Key, Group: entry.Group, Source: source}
	if entry.Sensitive {
		masked := ""
		if set {
			masked = maskedValue
		}
		item.Sensitive = &Sensitive{Set: set, Masked: masked, Source: source}
		return item
	}
	if set {
		item.Value = raw
	} else if entry.HasDefault {
		item.Value = entry.Default
	}
	return item
}

// MaxAnnouncementChars 是公告正文的字符上限，与旧部署一致。
const MaxAnnouncementChars = 200

// announcementLineBreaks 是公告正文中拒绝的换行与行分隔符。
// 字符集对齐旧线 env_io.has_line_break（Python str.splitlines）：
// CR、LF、VT、FF、U+001C..U+001E、NEL、U+2028、U+2029。
// 它们都会在单行存储里变成新的物理行。
const announcementLineBreaks = "\r\n\v\f\u001c\u001d\u001e\u0085\u2028\u2029"

// Announcement 是全站公告。UpdatedAt 为 nil 表示从未设置或已清空。
type Announcement struct {
	Text      string
	UpdatedAt *time.Time
}

// Store 是公告的持久化端口。空正文在存储侧删除行，
// 故清空后读到的 updatedAt 是 nil。返回值是原始列，不构造用例类型。
type Store interface {
	Announcement(ctx context.Context) (text string, updatedAt *time.Time, err error)
	SetAnnouncement(ctx context.Context, text, updatedBy string) (updatedAt *time.Time, err error)
}

// Deps 是公告用例的依赖。
type Deps struct {
	Store Store
	Audit appaudit.Repo
	Tx    app.TxManager
	Clock coreclock.Clock
}

// Service 是公告用例服务。
type Service struct {
	deps Deps
}

// NewService 构造公告服务。缺依赖即失败。
func NewService(deps Deps) (*Service, error) {
	if deps.Store == nil {
		return nil, errors.New("settings: nil announcement store")
	}
	if deps.Audit == nil {
		return nil, errors.New("settings: nil audit repo")
	}
	if deps.Tx == nil {
		return nil, errors.New("settings: nil transaction manager")
	}
	if deps.Clock == nil {
		return nil, errors.New("settings: nil clock")
	}
	return &Service{deps: deps}, nil
}

// Get 返回当前公告。未设置或已清空时 Text 为空、UpdatedAt 为 nil。
func (s *Service) Get(ctx context.Context) (Announcement, error) {
	text, updatedAt, err := s.deps.Store.Announcement(ctx)
	if err != nil {
		return Announcement{}, err
	}
	return Announcement{Text: text, UpdatedAt: updatedAt}, nil
}

// Set 发布或清空公告。
// 正文去首尾空白后必须单行且不超过 200 字符；空正文即清空。
// 写入与 settings.announcement 审计同事务；审计 detail 只有字段名。
func (s *Service) Set(ctx context.Context, raw string, actor appaudit.Actor) (Announcement, error) {
	text := strings.TrimSpace(raw)
	if strings.ContainsAny(text, announcementLineBreaks) {
		return Announcement{}, coreerrs.New(coreerrs.CodeValidation, "announcement must be a single line")
	}
	if len([]rune(text)) > MaxAnnouncementChars {
		return Announcement{}, coreerrs.New(coreerrs.CodeValidation,
			"announcement must be at most 200 characters")
	}
	var announcement Announcement
	err := s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		updatedAt, err := s.deps.Store.SetAnnouncement(ctx, text, actor.UserID)
		if err != nil {
			return err
		}
		announcement = Announcement{Text: text, UpdatedAt: updatedAt}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionSettingsAnnouncement,
			TargetType:  "settings",
			TargetID:    "announcement",
			Detail:      map[string]any{"field": "announcement"},
		})
		return err
	})
	if err != nil {
		return Announcement{}, err
	}
	return announcement, nil
}
