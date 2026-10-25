package protocol

import (
	"errors"
	"testing"
)

func TestParseDeviceStateFixture(t *testing.T) {
	state, err := ParseDeviceState(envelopeData(t, "device_state.json"))
	if err != nil {
		t.Fatalf("ParseDeviceState: %v", err)
	}
	if state.Authorized {
		t.Fatal("authorized = true, want false (registered but not authorized)")
	}
	if state.Code != "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90" {
		t.Fatalf("code = %q", state.Code)
	}
	if state.PhoneModel != "Example-EX1000" {
		t.Fatalf("phone model = %q", state.PhoneModel)
	}
	if state.UpdateTime == nil || *state.UpdateTime != 1761517844 {
		t.Fatalf("update time = %v", state.UpdateTime)
	}
}

func TestParseDeviceStateDefaults(t *testing.T) {
	state, err := ParseDeviceState(map[string]any{})
	if err != nil {
		t.Fatalf("ParseDeviceState: %v", err)
	}
	if state.Authorized || state.Code != "" || state.PhoneModel != "" || state.UpdateTime != nil {
		t.Fatalf("defaults = %+v", state)
	}
}

func TestParseDeviceStateRejectsNumericCode(t *testing.T) {
	_, err := ParseDeviceState(map[string]any{"Code": 123})
	var parse *ParseError
	if !errors.As(err, &parse) || parse.Field != "Code" {
		t.Fatalf("error = %v, want ParseError(Code)", err)
	}
}

func TestParseDeviceStateNonObject(t *testing.T) {
	for _, data := range []any{nil, "x", []any{}} {
		_, err := ParseDeviceState(data)
		var parse *ParseError
		if !errors.As(err, &parse) || parse.Field != "data" {
			t.Fatalf("%#v: error = %v, want ParseError(data)", data, err)
		}
	}
}

func TestParseDeviceStateAuthorizeVariants(t *testing.T) {
	cases := []struct {
		raw  any
		want bool
	}{
		{true, true},
		{false, false},
		{1, true},
		{0, false},
		{1.0, true},
		{"1", true},
		{"true", true},
		{"on", true},
		{"yes", true},
		{"no", false},
		{nil, false},
	}
	for _, tc := range cases {
		state, err := ParseDeviceState(map[string]any{"authorize": tc.raw})
		if err != nil {
			t.Fatalf("ParseDeviceState(%#v): %v", tc.raw, err)
		}
		if state.Authorized != tc.want {
			t.Fatalf("authorize %#v = %t, want %t", tc.raw, state.Authorized, tc.want)
		}
	}
}
