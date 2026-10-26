package window

import "errors"

// ErrInvalid 是窗口起止倒挂或零宽的错误。
var ErrInvalid = errors.New("window: start must be before end")
