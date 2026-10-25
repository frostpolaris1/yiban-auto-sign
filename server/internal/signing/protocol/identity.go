package protocol

import "fmt"

const defaultContainer = "StudentDefault"

// App 是身份载荷中的单个应用条目。
type App struct {
	ID        string
	ServiceID string
	Name      string
	URL       string
	AuthCode  string
}

// Identity 是 base/c/auth/yiban 的身份载荷。
type Identity struct {
	UniversityName string
	UniversityID   string
	PersonID       string
	PersonName     string
	PersonType     string
	State          *int
	Container      string
	Apps           []App
}

// ParseIdentity 解析身份载荷。
//
// 学校与人员的五个字段必填；State 双态收敛；Container 缺省为 StudentDefault；
// Apps 缺失即空列表。必填字段或 Apps 形态非法 → ParseError。
func ParseIdentity(data any) (Identity, error) {
	m, ok := data.(map[string]any)
	if !ok {
		return Identity{}, parseError("identity", data)
	}

	universityName, err := requiredString(m, "UniversityName")
	if err != nil {
		return Identity{}, err
	}
	universityID, err := requiredString(m, "UniversityId")
	if err != nil {
		return Identity{}, err
	}
	personID, err := requiredString(m, "PersonId")
	if err != nil {
		return Identity{}, err
	}
	personName, err := requiredString(m, "PersonName")
	if err != nil {
		return Identity{}, err
	}
	personType, err := requiredString(m, "PersonType")
	if err != nil {
		return Identity{}, err
	}

	container := defaultContainer
	if raw, ok := m["Container"]; ok {
		if s, ok := raw.(string); ok && s != "" {
			container = s
		} else if raw != nil && raw != "" {
			container = fmt.Sprint(raw)
		}
	}

	apps := []App{}
	if raw, ok := m["Apps"]; ok && raw != nil {
		list, ok := raw.([]any)
		if !ok {
			return Identity{}, parseError("Apps", raw)
		}
		apps = make([]App, 0, len(list))
		for _, item := range list {
			app, err := parseApp(item)
			if err != nil {
				return Identity{}, err
			}
			apps = append(apps, app)
		}
	}

	var state *int
	if n, ok := toInt(m["State"]); ok {
		state = &n
	}

	return Identity{
		UniversityName: universityName,
		UniversityID:   universityID,
		PersonID:       personID,
		PersonName:     personName,
		PersonType:     personType,
		State:          state,
		Container:      container,
		Apps:           apps,
	}, nil
}

func parseApp(item any) (App, error) {
	m, ok := item.(map[string]any)
	if !ok {
		return App{}, parseError("Apps", item)
	}
	required := []string{"Id", "ServiceId", "AppName", "AppUrl"}
	for _, key := range required {
		if v, ok := m[key]; !ok || v == nil {
			return App{}, parseError("Apps[]."+key, m)
		}
	}
	authCode := ""
	if raw, ok := m["AuthCode"]; ok && raw != nil {
		if s, ok := raw.(string); ok {
			authCode = s
		} else {
			authCode = fmt.Sprint(raw)
		}
	}
	return App{
		ID:        fmt.Sprint(m["Id"]),
		ServiceID: fmt.Sprint(m["ServiceId"]),
		Name:      fmt.Sprint(m["AppName"]),
		URL:       fmt.Sprint(m["AppUrl"]),
		AuthCode:  authCode,
	}, nil
}
