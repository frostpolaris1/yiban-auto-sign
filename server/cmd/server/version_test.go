package main

// --version 参数判据。
//
// # 口径
// 只认 --version 与 -version；其他参数一律用法错误（退出码 2）。
// 版本值本身的格式与占位符由 internal/buildinfo 的单测钉住。

import "testing"

func TestIsVersionArg(t *testing.T) {
	cases := map[string]bool{
		"--version": true,
		"-version":  true,
		"version":   false,
		"--help":    false,
		"":          false,
	}
	for arg, want := range cases {
		if got := isVersionArg(arg); got != want {
			t.Fatalf("isVersionArg(%q) = %v, want %v", arg, got, want)
		}
	}
}
