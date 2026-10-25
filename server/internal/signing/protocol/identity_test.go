package protocol

import (
	"errors"
	"testing"
)

func TestParseIdentityFixture(t *testing.T) {
	identity, err := ParseIdentity(envelopeData(t, "auth_response.json"))
	if err != nil {
		t.Fatalf("ParseIdentity: %v", err)
	}
	if identity.UniversityName != "示例大学" ||
		identity.UniversityID != "00000000000000000000000000000001" ||
		identity.PersonID != "00000000000000000000000000000004" ||
		identity.PersonName != "张三" ||
		identity.PersonType != "student" {
		t.Fatalf("identity = %+v", identity)
	}
	if identity.State == nil || *identity.State != 1 {
		t.Fatalf("state = %v, want 1", identity.State)
	}
	if identity.Container != "StudentDefault" {
		t.Fatalf("container = %q", identity.Container)
	}
	if len(identity.Apps) != 3 {
		t.Fatalf("apps = %d, want 3", len(identity.Apps))
	}
	app := identity.Apps[0]
	if app.ID != "ddff3b902972f25df78087d9fd004772" ||
		app.ServiceID != "1ab8139127501809e2b58a72587b5037" ||
		app.Name != "早操签到" ||
		app.URL != "https://app.uyiban.com/nightattendance/student/" ||
		app.AuthCode != "nightattendance.student.*" {
		t.Fatalf("first app = %+v", app)
	}
}

func TestParseIdentityRequiresFields(t *testing.T) {
	for _, key := range []string{
		"UniversityName", "UniversityId", "PersonId", "PersonName", "PersonType",
	} {
		data := envelopeData(t, "auth_response.json").(map[string]any)
		delete(data, key)
		_, err := ParseIdentity(data)
		var parse *ParseError
		if !errors.As(err, &parse) || parse.Field != key {
			t.Fatalf("missing %s: error = %v", key, err)
		}
	}
}

func TestParseIdentityDefaults(t *testing.T) {
	data := envelopeData(t, "auth_response.json").(map[string]any)
	delete(data, "Apps")
	delete(data, "Container")
	delete(data, "State")
	identity, err := ParseIdentity(data)
	if err != nil {
		t.Fatalf("ParseIdentity: %v", err)
	}
	if len(identity.Apps) != 0 {
		t.Fatalf("apps = %d, want 0", len(identity.Apps))
	}
	if identity.Container != "StudentDefault" {
		t.Fatalf("container = %q", identity.Container)
	}
	if identity.State != nil {
		t.Fatalf("state = %v, want nil", identity.State)
	}
}

func TestParseIdentityStringState(t *testing.T) {
	data := envelopeData(t, "auth_response.json").(map[string]any)
	data["State"] = "1"
	identity, err := ParseIdentity(data)
	if err != nil {
		t.Fatalf("ParseIdentity: %v", err)
	}
	if identity.State == nil || *identity.State != 1 {
		t.Fatalf("state = %v, want 1", identity.State)
	}
}

func TestParseIdentityRejectsBadApps(t *testing.T) {
	data := envelopeData(t, "auth_response.json").(map[string]any)
	data["Apps"] = "not-a-list"
	_, err := ParseIdentity(data)
	var parse *ParseError
	if !errors.As(err, &parse) || parse.Field != "Apps" {
		t.Fatalf("error = %v, want ParseError(Apps)", err)
	}
}
