// Package buildinfo 是 Go 二进制的版本身份。
//
// # 功能
// 提供 version、commit、build_time 三元组与稳定文本渲染；
// 三个值由构建期 -ldflags -X 注入。
// 本包**不**读 git、**不**读环境、**不**写文件、**不**联网。
//
// # 归属
// 编译期载体包，无依赖。cmd/server 与 cmd/cli 共用同一份定义，
// 保证 --version 与 version 子命令只有一处事实源。
//
// # 复用
// Current、Info、Info.String。
//
// # 口径
// 未注入时 version=dev、commit=unknown、build_time=unknown：
// 缺构建信息必须显性，不编造 sha 或时间。空字符串同样回落占位符。
// -X 的注入名是 **Go 标识符**（version、commit、buildTime），
// 不是输出字段名（build_time）；改 identifier 前先改发布脚本：
//
//	go build -ldflags "-X github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo.version=1.0.0 \
//	  -X github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo.commit=$(git rev-parse --short HEAD) \
//	  -X github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo.buildTime=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
//
// build_time 的官方构建口径是 RFC3339 UTC；本包不解析该值。
package buildinfo

import (
	"fmt"
	"strings"
)

// 构建期变量。只允许 -ldflags -X 改写；包内测试直接赋值。
var (
	version   = "dev"
	commit    = "unknown"
	buildTime = "unknown"
)

// Info 是一个二进制的版本身份。
type Info struct {
	Version   string
	Commit    string
	BuildTime string
}

// Current 返回本二进制的身份。
func Current() Info {
	return normalize(Info{Version: version, Commit: commit, BuildTime: buildTime})
}

// normalize 把空白注入值换成显性占位符。
func normalize(i Info) Info {
	i.Version = strings.TrimSpace(i.Version)
	if i.Version == "" {
		i.Version = "dev"
	}
	i.Commit = strings.TrimSpace(i.Commit)
	if i.Commit == "" {
		i.Commit = "unknown"
	}
	i.BuildTime = strings.TrimSpace(i.BuildTime)
	if i.BuildTime == "" {
		i.BuildTime = "unknown"
	}
	return i
}

// String 渲染三行稳定文本，供 --version 与 version 子命令使用。
func (i Info) String() string {
	i = normalize(i)
	return fmt.Sprintf("version: %s\ncommit: %s\nbuild_time: %s\n", i.Version, i.Commit, i.BuildTime)
}
