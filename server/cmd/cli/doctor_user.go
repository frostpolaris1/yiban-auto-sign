// doctor 的跨平台用户名读取。
package main

import (
	"os/user"
)

// currentUserName 返回当前进程用户名。ok=false 表示平台拿不到。
func currentUserName() (string, bool) {
	u, err := user.Current()
	if err != nil || u.Username == "" {
		return "", false
	}
	return u.Username, true
}
