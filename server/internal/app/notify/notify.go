// Package notify 实现每用户通知订阅用例（2ws A）。
//
// # 功能
// 读、写当前管理员自己的订阅覆盖集；缺行按默认解释。
// 类别与渠道是闭合词表；未知值或重复对一律校验失败。
// 本包**不**投递站内信、**不**发邮件、**不**写审计。
//
// # 归属
// app 层用例。端口见本文件，adapters/pg 实现；
// 本批只存取订阅，投递随后续批次落地。
//
// # 复用
// Service、Page、Repo；类型别名 Category、Channel、Pair、Pref 与
// 常量 Category*、Channel* 都来自 core/notify。
//
// # 口径
// 存储只放覆盖值，不放默认值：PUT 的 items 就是完整覆盖集，
// 未列出的对随之回到默认。默认表在 core/notify 只有一份。
// 本表是**管理员订阅**面；用户本人的签到失败邮件总开关是
// users.mail_notify。0v8 投递用户邮件要求 mail_notify=true，
// 管理员摘要只看本表，两者分别判定。
package notify

import (
	"context"
	"errors"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

// Category 是通知主题的闭合词表。
type Category = corenotify.Category

// Channel 是投递渠道的闭合词表。
type Channel = corenotify.Channel

// 类别与渠道常量转发自 core/notify。
const (
	CategoryCredentialInvalid = corenotify.CategoryCredentialInvalid
	CategoryReviewPending     = corenotify.CategoryReviewPending
	CategorySignFailedDaily   = corenotify.CategorySignFailedDaily
	CategoryCapacityRejected  = corenotify.CategoryCapacityRejected

	ChannelInApp = corenotify.ChannelInApp
	ChannelEmail = corenotify.ChannelEmail
)

// Pair 是一个（类别, 渠道）组合。
type Pair = corenotify.Pair

// Pref 是一对订阅的有效状态。
type Pref = corenotify.Pref

// Default 返回一对的默认状态。
func Default(pair Pair) bool { return corenotify.Default(pair) }

// Repo 是订阅覆盖表的持久化端口。
//
// Overrides 只返回已存的覆盖行；缺行**不得**用默认补齐，
// 否则「未覆盖」与「显式设为默认值」在存储里无法区分，
// PUT 的完整替换语义就会漂移。
// Replace 必须原子替换该用户的全部覆盖行：删除旧行与写入新行同事务。
type Repo interface {
	Overrides(ctx context.Context, userID string) (map[Pair]bool, error)
	Replace(ctx context.Context, userID string, prefs []Pref) error
}

// Page 是全部八对的有效状态，顺序稳定。
type Page struct {
	Items []Pref
}

// Service 是订阅用例服务。
type Service struct {
	repo Repo
}

// NewService 构造服务。缺仓储即失败。
func NewService(repo Repo) (*Service, error) {
	if repo == nil {
		return nil, errors.New("notify: nil repo")
	}
	return &Service{repo: repo}, nil
}

// Get 返回 userID 的全部有效订阅。缺行按默认解释。
func (s *Service) Get(ctx context.Context, userID string) (Page, error) {
	if userID == "" {
		return Page{}, errs.New(errs.CodeInternal, "notification preferences require a user")
	}
	overrides, err := s.repo.Overrides(ctx, userID)
	if err != nil {
		return Page{}, err
	}
	return Page{Items: effective(overrides)}, nil
}

// Set 幂等替换 userID 的覆盖集，并返回替换后的有效状态。
// 未知类别/渠道或重复对即校验失败；空列表回到全默认。
func (s *Service) Set(ctx context.Context, userID string, items []Pref) (Page, error) {
	if userID == "" {
		return Page{}, errs.New(errs.CodeInternal, "notification preferences require a user")
	}
	if items == nil {
		return Page{}, errs.New(errs.CodeValidation, "items is required")
	}
	seen := make(map[Pair]bool, len(items))
	for _, item := range items {
		if !item.Category.Valid() {
			return Page{}, errs.New(errs.CodeValidation, "unknown notification category")
		}
		if !item.Channel.Valid() {
			return Page{}, errs.New(errs.CodeValidation, "unknown notification channel")
		}
		pair := Pair{Category: item.Category, Channel: item.Channel}
		if seen[pair] {
			return Page{}, errs.New(errs.CodeValidation, "duplicate notification subscription")
		}
		seen[pair] = true
	}
	if err := s.repo.Replace(ctx, userID, items); err != nil {
		return Page{}, err
	}
	overrides := make(map[Pair]bool, len(items))
	for _, item := range items {
		overrides[Pair{Category: item.Category, Channel: item.Channel}] = item.Enabled
	}
	return Page{Items: effective(overrides)}, nil
}

// effective 把覆盖表展开成全部八对；缺行取默认。
func effective(overrides map[Pair]bool) []Pref {
	categories := corenotify.Categories()
	channels := corenotify.Channels()
	items := make([]Pref, 0, len(categories)*len(channels))
	for _, category := range categories {
		for _, channel := range channels {
			pair := Pair{Category: category, Channel: channel}
			enabled, ok := overrides[pair]
			if !ok {
				enabled = Default(pair)
			}
			items = append(items, Pref{Category: category, Channel: channel, Enabled: enabled})
		}
	}
	return items
}
