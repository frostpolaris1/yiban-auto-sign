// Package egress 是出口标识的唯一定义。
//
// # 功能
// 把代理串归一为 scheme://host[:port]，并去掉 userinfo。
// 本包**不**建连接、**不**探测代理可用性、**不**做限速。
//
// # 归属
// core 层纯领域包。风控按出口 IP 计数，
// 故出口标识必须全仓同名同形。
//
// # 复用
// Identity、Record，以及常量 Direct、Unparseable。
//
// # 口径
// 空串与纯空白都算直连（Direct）。
// 无法解析的代理串归入 Unparseable，**不退回 Direct**。
package egress

import (
	"net/url"
	"strings"
)

const (
	// Direct 是直连的标识。
	Direct = "direct"
	// Unparseable 是畸形代理串的标识。
	Unparseable = "<unparseable>"
)

// Identity 把代理串归一为 scheme://host[:port]，去掉 userinfo。
// 归一结果参与风控计数，**同一代理的不同写法必须得到同一串**。
func Identity(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return Direct
	}
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return Unparseable
	}
	u.User = nil
	return u.Scheme + "://" + u.Host
}

// Record 是一个出口记录：ID 是稳定主键，Identity 是归一后的出口标识。
// 定义在 core：app 端口与 pg 适配器共用同一取值对象，适配器不导入 app。
type Record struct {
	ID       string
	Identity string
}
