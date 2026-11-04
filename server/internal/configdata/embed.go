// Package configdata 只持有 Go 服务端的配置名册字节。
//
// # 功能
// 编译期把 registry.json 嵌入二进制，启动与体检读同一份名册：
// 名册不随安装位、工作目录或镜像层变化，也不存在"文件缺失"的降级路径。
// 本包**不**解析名册、**不**读取环境变量、**不**判断运行环境；
// 形状校验与语义在 internal/adapters/config。
//
// # 归属
// 无依赖的叶子包：只被 internal/adapters/config 导入。
// 单独成包是为了让 go:embed 落在包内（embed 不能引用包外文件），
// 同时不把 JSON 数据混进 adapters 的代码目录。
//
// # 复用
// JSON。
//
// # 口径
// 名册只登记 Go 服务端实际读取的键（唯一的读取点是
// internal/adapters/config）。仓库根 config/ 下的旧名册与本包无关，
// 它只服务旧 Python 栈，不得往本名册里补旧键。
package configdata

import _ "embed"

//go:embed registry.json
var registryJSON string

// JSON 返回嵌入的名册原文。每次调用返回新副本，调用方可以安全改写。
func JSON() []byte { return []byte(registryJSON) }
