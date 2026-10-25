import { Card } from '@/components/ui/Card'
import { SelectInput, TextAreaInput, TextInput } from '@/components/ui/Input'

/** Form controls. Classes and DOM follow Adminator `src/forms.html`. */
export function FormsSection() {
  return (
    <Card className="col-6" eyebrow="Forms" title="输入">
      <div className="form-grid">
        <TextInput id="demo-name" label="显示名" placeholder="输入名称" hint="用于列表展示，最多 32 字。" />
        <TextInput
          id="demo-email"
          label="邮箱"
          type="email"
          icon="mail"
          placeholder="name@example.com"
          defaultValue="not-an-email"
          error="请输入有效邮箱地址。"
        />
        <TextInput id="demo-endpoint" label="端点" addon="https://" placeholder="example.com" />
        <TextInput id="demo-locked" label="只读" defaultValue="Read only" disabled />
        <SelectInput
          id="demo-role"
          label="角色"
          defaultValue="admin"
          options={[
            { value: 'master', label: 'master' },
            { value: 'admin', label: 'admin' },
            { value: 'user', label: 'user' },
          ]}
        />
        <SelectInput
          id="demo-locked-select"
          label="锁定计划"
          defaultValue="locked"
          disabled
          options={[{ value: 'locked', label: 'Locked plan' }]}
        />
        <div className="span-2">
          <TextAreaInput
            id="demo-note"
            label="备注"
            placeholder="一段说明…"
            hint="仅前端演示，不落库。"
          />
        </div>
        <div className="span-2">
          <div className="demo-row">
            <label className="check">
              <input type="checkbox" defaultChecked />
              <span className="box" />
              订阅变更日志
            </label>
            <label className="check">
              <input type="checkbox" />
              <span className="box" />
              每周产品提示
            </label>
            <label className="check">
              <input type="checkbox" disabled />
              <span className="box" />
              短信通知（未开放）
            </label>
          </div>
          <div className="section-h" style={{ margin: '18px 0 10px', fontSize: 13 }}>
            计划 <span className="num">03</span>
          </div>
          <div className="demo-row">
            <label className="check radio">
              <input type="radio" name="demo-plan" defaultChecked />
              <span className="box" />
              标准
            </label>
            <label className="check radio">
              <input type="radio" name="demo-plan" />
              <span className="box" />
              旗舰
            </label>
            <label className="check radio">
              <input type="radio" name="demo-plan" />
              <span className="box" />
              团队
            </label>
          </div>
        </div>
      </div>
    </Card>
  )
}
