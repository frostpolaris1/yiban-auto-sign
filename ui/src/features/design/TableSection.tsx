import { useState } from 'react'
import { Icon } from '@/components/ui/Icon'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { DataTable, type DataTableColumn } from '@/components/ui/DataTable'

type AccountRow = {
  id: string
  initials: string
  name: string
  email: string
  role: 'master' | 'admin' | 'user'
  school: string
  status: '已签到' | '等待中' | '凭据错误'
  time: string
}

const ROWS: AccountRow[] = [
  {
    id: 'ACC-1042',
    initials: 'SK',
    name: 'Sara Kim',
    email: 'sara.kim@example.com',
    role: 'admin',
    school: '北校区',
    status: '已签到',
    time: '06:41',
  },
  {
    id: 'ACC-1043',
    initials: 'MD',
    name: 'Moo Doe',
    email: 'moo.doe@example.com',
    role: 'user',
    school: '北校区',
    status: '等待中',
    time: '06:52',
  },
  {
    id: 'ACC-2088',
    initials: 'LD',
    name: 'Lee Doe',
    email: 'lee.doe@example.com',
    role: 'user',
    school: '南校区',
    status: '凭据错误',
    time: '—',
  },
  {
    id: 'ACC-2090',
    initials: 'JT',
    name: 'Jin Tao',
    email: 'jin.tao@example.com',
    role: 'user',
    school: '南校区',
    status: '已签到',
    time: '07:03',
  },
  {
    id: 'ACC-2104',
    initials: 'YW',
    name: 'Yu Wei',
    email: 'yu.wei@example.com',
    role: 'user',
    school: '北校区',
    status: '已签到',
    time: '07:11',
  },
]

const ROLE_TONE = {
  master: 'purple',
  admin: 'primary',
  user: 'info',
} as const

const STATUS_TONE = {
  已签到: 'success',
  等待中: 'warning',
  凭据错误: 'danger',
} as const

const COLUMNS: Array<DataTableColumn<AccountRow>> = [
  {
    key: 'user',
    header: '账号',
    sorted: 'asc',
    render: (row) => (
      <div className="data-cell-user">
        <div className="av ma-2">{row.initials}</div>
        <div className="data-cell-user-meta">
          <div className="data-cell-user-name">{row.name}</div>
          <div className="data-cell-user-email">{row.email}</div>
        </div>
      </div>
    ),
  },
  {
    key: 'role',
    header: '角色',
    render: (row) => <Badge tone={ROLE_TONE[row.role]}>{row.role}</Badge>,
  },
  { key: 'school', header: '学校', render: (row) => row.school },
  {
    key: 'status',
    header: '状态',
    render: (row) => (
      <Badge tone={STATUS_TONE[row.status]} dot>
        {row.status}
      </Badge>
    ),
  },
  { key: 'id', header: 'ID', render: (row) => <span className="data-cell-mono">{row.id}</span> },
  {
    key: 'time',
    header: '最近活动',
    render: (row) => <span className="data-cell-mono">{row.time}</span>,
  },
  {
    key: 'actions',
    header: '',
    align: 'right',
    render: (row) => (
      <div className="data-cell-actions">
        <button type="button" className="btn--icon" aria-label={`查看 ${row.id}`}>
          <Icon name="eye" />
        </button>
        <button type="button" className="btn--icon" aria-label={`编辑 ${row.id}`}>
          <Icon name="edit" />
        </button>
      </div>
    ),
  },
]

/** Data table card. DOM follows Adminator `src/datatable.html`. */
export function TableSection() {
  const [page, setPage] = useState(1)

  return (
    <Card
      className="col-12"
      eyebrow="Data"
      title="表格"
      action={<span className="app-caption">服务端分页 · 每页 {ROWS.length} 条</span>}
    >
      <DataTable
        caption="账号列表"
        columns={COLUMNS}
        rows={ROWS}
        rowKey={(row) => row.id}
        page={page}
        pageSize={ROWS.length}
        total={42}
        onPageChange={setPage}
        toolbar={
          <>
            <div className="data-toolbar-left">
              <div className="input-icon" style={{ flex: 1, maxWidth: 320 }}>
                <span className="ico">
                  <Icon name="search" />
                </span>
                <input
                  className="input"
                  type="search"
                  placeholder="搜索账号、邮箱或 ID…"
                  aria-label="搜索账号"
                />
              </div>
              <button type="button" className="btn btn--ghost">
                <Icon name="filter" />
                筛选
                <span className="badge primary" style={{ marginLeft: 4 }}>
                  2
                </span>
              </button>
            </div>
            <div className="data-toolbar-right">
              <select
                className="select"
                aria-label="角色筛选"
                defaultValue="all"
                style={{ width: 'auto', padding: '7px 28px 7px 10px', fontSize: 12 }}
              >
                <option value="all">全部角色</option>
                <option value="master">master</option>
                <option value="admin">admin</option>
                <option value="user">user</option>
              </select>
              <select
                className="select"
                aria-label="状态筛选"
                defaultValue="all"
                style={{ width: 'auto', padding: '7px 28px 7px 10px', fontSize: 12 }}
              >
                <option value="all">全部状态</option>
                <option value="done">已签到</option>
                <option value="wait">等待中</option>
                <option value="error">凭据错误</option>
              </select>
              <Button variant="ghost" size="icon" aria-label="列设置">
                <Icon name="columns" />
              </Button>
            </div>
          </>
        }
      />
    </Card>
  )
}
