package yiban

// 平台固定值。它们来自客户端安装包与实拍，改动等于换登录方式。
const (
	appVersion    = "5.2.3"
	oauthClientID = "95626fa3080300ea"
	redirectURI   = "https://f.yiban.cn/iapp7463"
	iframeAct     = "iapp7463"
	loginOrigin   = "https://c.uyiban.com"
	appOrigin     = "https://app.uyiban.com"
)

// Endpoints 是登录链与会话请求的端点。
// 测试注入 httptest 的 URL；生产用 DefaultEndpoints。
type Endpoints struct {
	CodeHTML     string
	Usersure     string
	IframeIndex  string
	APIAuth      string
	SignPosition string
	SignIn       string
	DeviceState  string
}

// DefaultEndpoints 返回平台默认端点。
func DefaultEndpoints() Endpoints {
	return Endpoints{
		CodeHTML:     "https://oauth.yiban.cn/code/html",
		Usersure:     "https://oauth.yiban.cn/code/usersure",
		IframeIndex:  "https://f.yiban.cn/iframe/index",
		APIAuth:      "https://api.uyiban.com/base/c/auth/yiban",
		SignPosition: "https://api.uyiban.com/nightAttendance/student/index/signPosition",
		SignIn:       "https://api.uyiban.com/nightAttendance/student/index/signIn",
		DeviceState:  "https://api.uyiban.com/device/student/index/getState",
	}
}
