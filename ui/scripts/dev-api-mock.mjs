/**
 * 仅开发用的 API 假人：`npm run dev:mock`（vite --mode devmock）。
 *
 * 功能：
 * 没有 Go 后端也能完整预览认证页与用户端——注册开关、口令策略、
 * 注册即登录、会话 me、更新日志；登录后播种一条合成签到账号，
 * 账号页/新建页的读面与动作（detect/创建/编辑/暂停/验证/刷新/删除/恢复/照片）最小闭环。
 *
 * 口径：
 * 只在 devmock 模式的 dev server 生效；构建、常规 dev（真代理）与 e2e 都不走这里。
 * 数据与 e2e fake 同族合成值（acc-user/school-a/138****8000），零真实数据。
 * me 会话由内存标记维持：register/login 置位，logout 清除；刷新即失（无 cookie）。
 * 路径以 `src/lib/api/*` 的 PATHS 为准（/accounts 而非 /me/accounts）。
 */

const send = (res, body, status = 200) => {
  res.writeHead(status, { 'content-type': 'application/json' })
  res.end(JSON.stringify(body))
}

const readBody = (req) =>
  new Promise((resolve) => {
    let raw = ''
    req.on('data', (chunk) => (raw += chunk))
    req.on('end', () => {
      try {
        resolve(JSON.parse(raw))
      } catch {
        resolve({})
      }
    })
  })

const sessionUser = (email) => ({
  id: 'user-demo',
  email,
  role: 'user',
  status: 'active',
  must_change_password: false,
  csrf_token: 'csrf-dev',
})

const NOW = '2026-11-12T07:02:00+08:00'

const seedAccount = () => ({
  id: 'acc-user',
  school_id: 'school-a',
  owner_user_id: 'user-demo',
  owner_email_masked: 'd***@example.com',
  name: '东区宿舍',
  display_name: '东区宿舍',
  phone_masked: '138****8000',
  phone_model: 'Pixel 7',
  status: 'active',
  deleted_at: null,
  reject_reason: '',
  reviewed_at: null,
  reviewed_by: null,
  created_at: '2026-10-01T01:00:00Z',
  updated_at: NOW,
})

const SCHOOLS = [
  { id: 'school-a', name: '合成学校甲', code: 'YB10001' },
  { id: 'school-b', name: '合成学校乙', code: 'YB10002' },
  { id: 'school-c', name: '实验学校', code: 'YB10003' },
]

const seedPhotos = () => [
  { id: 'photo-a', sha256: 'synthetic-a', mime: 'image/png', width: 800, height: 600, bytes: 1024, created_at: NOW },
  { id: 'photo-b', sha256: 'synthetic-b', mime: 'image/png', width: 800, height: 600, bytes: 1024, created_at: NOW },
]

// 合成照片字节：渐变 SVG 占位，浏览器 <img> 可直接渲染。
const photoSvg = (id) => {
  const hue = id.endsWith('b') ? '210' : '222'
  return `<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="hsl(${hue} 70% 82%)"/><stop offset="1" stop-color="hsl(${hue} 65% 64%)"/></linearGradient></defs><rect width="800" height="600" fill="url(#g)"/></svg>`
}

const signStatus = (account) => ({
  account_id: account.id,
  display_name: account.display_name,
  phone_masked: account.phone_masked,
  account_status: account.status,
  state: account.status === 'active' ? 'success' : 'skipped',
  run_at: NOW,
  stale: false,
})

const diagnostics = (account) => ({
  ...signStatus(account),
  attempts: 1,
  result: 'ok',
  next_retry_at: null,
  suggestion: 'wait',
})

const TIME_PREF = {
  offset_min: 15,
  allowed: true,
  window: { start: '06:30', end: '07:50' },
  slots: Array.from({ length: 16 }, (_, index) => {
    const minutes = 6 * 60 + 30 + index * 5
    const label = `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`
    return { offset_min: index * 5, label, disabled: false, pct: index === 3 ? 40 : 0, edge_note: '' }
  }),
  estimated: '06:45',
  estimate_note: '按当前容量估算',
}

function calendar(from, to) {
  const days = []
  const cursor = new Date(`${from}T00:00:00Z`)
  const end = new Date(`${to}T00:00:00Z`)
  while (cursor <= end) {
    days.push({
      day: cursor.toISOString().slice(0, 10),
      total: 1,
      success: 1,
      failed: 0,
      skipped: 0,
      unknown: 0,
      future: false,
    })
    cursor.setUTCDate(cursor.getUTCDate() + 1)
  }
  return { timezone: 'Asia/Shanghai', today: to, updated_at: NOW, days }
}

export function devApiMock() {
  return {
    name: 'dev-api-mock',
    configureServer(server) {
      if (server.config.mode !== 'devmock') return
      let loggedEmail = null
      let account = null
      let photos = []
      let mailEnabled = true
      let retentionDays = 90
      let timeOffsetMin = 15
      // 与 mail/retention 同族：PUT 持久化，GET 回读，读数不撒谎。
      const timePref = () => {
        const slot = TIME_PREF.slots.find((s) => s.offset_min === timeOffsetMin) ?? null
        return {
          ...TIME_PREF,
          offset_min: timeOffsetMin,
          estimated: slot?.label ?? TIME_PREF.estimated,
        }
      }

      const login = (email) => {
        loggedEmail = email
        account = seedAccount()
        photos = seedPhotos()
      }

      server.middlewares.use('/api', async (req, res) => {
        const path = (req.url ?? '').split('?')[0]
        const query = new URLSearchParams((req.url ?? '').split('?')[1] ?? '')
        const method = req.method ?? 'GET'

        if (path === '/auth/registration') {
          return send(res, { enabled: true, terms_version: 1, privacy_version: 1 })
        }
        if (path === '/auth/password-policy') {
          return send(res, { min_chars: 10, min_classes: 2, max_bytes: 1024 })
        }
        if (path === '/auth/me') {
          return loggedEmail ? send(res, sessionUser(loggedEmail)) : send(res, { code: 'unauthorized', message: 'unauthorized' }, 401)
        }
        if (path === '/auth/login' && method === 'POST') {
          const body = await readBody(req)
          login(body.email ?? 'demo@example.com')
          return send(res, sessionUser(loggedEmail))
        }
        if (path === '/auth/register' && method === 'POST') {
          const body = await readBody(req)
          login(body.email ?? 'demo@example.com')
          return send(res, sessionUser(loggedEmail), 201)
        }
        if (path === '/auth/logout') {
          loggedEmail = null
          account = null
          photos = []
          res.writeHead(204)
          return res.end()
        }
        if (path === '/changelog') {
          return send(res, { text: '# CHANGELOG（dev mock）\n\n## v0.1.0\n- 本地预览合成条目' })
        }

        if (!loggedEmail) {
          return send(res, { code: 'unauthorized', message: 'unauthorized' }, 401)
        }

        // ---- 学校 ----
        if (path === '/schools') return send(res, { items: SCHOOLS, next_cursor: '' })

        // ---- 账号读面 ----
        if (path === '/accounts' && method === 'GET') {
          const items = account && (query.get('include_deleted') === 'true' || account.deleted_at === null) ? [account] : []
          return send(res, { items, next_cursor: '' })
        }
        if (path === '/accounts/summary') return send(res, { pending: 0, rejected: 0 })
        if (path === '/accounts/detect' && method === 'POST') {
          const body = await readBody(req)
          if (!body.phone || !body.password) {
            return send(res, { code: 'bad_request', message: 'phone and password required' }, 400)
          }
          return send(res, {
            university_name: '合成大学甲',
            university_id: 'univ-a',
            school: { id: 'school-a', name: '合成学校甲' },
            person_name: '合成人',
            device_model: 'Pixel 7',
            device_code_present: true,
          })
        }
        if (path === '/accounts' && method === 'POST') {
          const body = await readBody(req)
          account = {
            ...seedAccount(),
            id: 'acc-new',
            school_id: body.school_id ?? 'school-a',
            name: body.name ?? '',
            display_name: body.name ?? '',
            phone_model: body.phone_model ?? '',
            status: 'pending',
            created_at: NOW,
            updated_at: NOW,
          }
          return send(res, account, 201)
        }

        const accountMatch = path.match(/^\/accounts\/([^/]+)(\/.*)?$/)
        if (accountMatch && account) {
          const sub = accountMatch[2] ?? ''
          if (sub === '/photos' && method === 'GET') return send(res, { items: photos, next_cursor: '' })
          if (sub === '/location') return send(res, { mode: 'auto', lng: null, lat: null, reason: '' })
          if (sub === '/pause' && method === 'POST') {
            const body = await readBody(req)
            account = { ...account, status: body.paused ? 'paused_user' : 'active', updated_at: NOW }
            return send(res, account)
          }
          if (sub === '/verify' && method === 'POST') {
            return send(res, { ok: true, code: 'ok', identity: { university_name: '合成大学甲' } })
          }
          if (sub === '/person-name' && method === 'POST') {
            return send(res, { person_name: '合成人' })
          }
          if (sub === '/device-refresh' && method === 'POST') {
            const body = await readBody(req)
            if (!body.current_password) return send(res, { code: 'bad_request', message: 'current_password required' }, 400)
            return send(res, { device_code_masked: 'YB-D3****', phone_model: account.phone_model, authorized: true })
          }
          if (sub === '' && method === 'PATCH') {
            const body = await readBody(req)
            const credentials = body.phone !== undefined || body.password !== undefined || body.phone_model !== undefined
            if (credentials && !body.current_password) {
              return send(res, { code: 'bad_request', message: 'current_password required' }, 400)
            }
            account = {
              ...account,
              name: body.name ?? account.name,
              display_name: body.name ?? account.display_name,
              phone_model: body.phone_model ?? account.phone_model,
              status: credentials ? 'pending' : account.status,
              updated_at: NOW,
            }
            return send(res, account)
          }
        }

        const selfMatch = path.match(/^\/me\/accounts\/([^/]+)\/(delete|restore)$/)
        if (selfMatch && method === 'POST' && account) {
          const body = await readBody(req)
          if (!body.current_password) return send(res, { code: 'bad_request', message: 'current_password required' }, 400)
          account = {
            ...account,
            deleted_at: selfMatch[2] === 'delete' ? NOW : null,
            status: selfMatch[2] === 'delete' ? account.status : 'active',
            updated_at: NOW,
          }
          res.writeHead(204)
          return res.end()
        }

        // ---- 照片 ----
        if (path === '/me/photos' && method === 'POST') {
          req.on('data', () => {})
          await new Promise((resolve) => req.on('end', resolve))
          const photo = { id: `photo-${photos.length + 1}`, sha256: 'synthetic-new', mime: 'image/png', width: 800, height: 600, bytes: 2048, created_at: NOW }
          photos = [...photos, photo]
          return send(res, photo, 201)
        }
        const photoMatch = path.match(/^\/me\/photos\/([^/]+)$/)
        if (photoMatch) {
          if (method === 'DELETE') {
            photos = photos.filter((item) => item.id !== photoMatch[1])
            res.writeHead(204)
            return res.end()
          }
          res.writeHead(200, { 'content-type': 'image/svg+xml' })
          return res.end(photoSvg(photoMatch[1]))
        }

        // ---- 首页/设置读面 ----
        if (path === '/me/sign-status') return send(res, { items: account && account.deleted_at === null ? [signStatus(account)] : [] })
        if (path === '/me/diagnostics') return send(res, { items: account && account.deleted_at === null ? [diagnostics(account)] : [] })
        if (path === '/me/time-pref') {
          if (method === 'PUT') {
            const body = await readBody(req)
            const off = body.offset_min
            if (off === null || TIME_PREF.slots.some((s) => s.offset_min === off && !s.disabled)) {
              timeOffsetMin = off
            }
          }
          return send(res, timePref())
        }
        if (path === '/me/sign-calendar') {
          const to = query.get('to') ?? '2026-11-12'
          const from = query.get('from') ?? '2026-11-06'
          return send(res, calendar(from, to))
        }
        const dayMatch = path.match(/^\/me\/sign-calendar\/(\d{4}-\d{2}-\d{2})$/)
        if (dayMatch) {
          return send(res, {
            day: dayMatch[1],
            timezone: 'Asia/Shanghai',
            future: false,
            accounts: account ? [{ account_id: account.id, state: 'success', display_name: account.display_name, phone_masked: account.phone_masked, run_at: NOW, attempts: 1 }] : [],
          })
        }
        if (path === '/me/mail-notify') {
          if (method === 'PUT') {
            const body = await readBody(req)
            mailEnabled = Boolean(body.enabled)
          }
          return send(res, { enabled: mailEnabled })
        }
        if (path === '/me/photo-retention') {
          if (method === 'PUT') {
            const body = await readBody(req)
            if ([30, 90, 180, 365].includes(body.days)) retentionDays = body.days
          }
          return send(res, { days: retentionDays })
        }
        if (path === '/announcement') return send(res, { text: '', updated_at: null })

        return send(res, { code: 'not_found', message: `dev-mock 未注册：${path}` }, 404)
      })
    },
  }
}