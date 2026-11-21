import assert from 'node:assert/strict'
import test from 'node:test'

import { buildNotificationSearch, NOTIFICATION_PAGE_LIMIT } from '../../src/lib/api/notify.ts'
import { INBOX_VIEW_ROLES, roleAllowed } from '../../src/lib/roles.ts'
import { showcaseNotifications, showcaseUnreadCount } from '../../src/lib/showcase.ts'
import {
  categoryMeta,
  flattenUniqueNotifications,
  formatNotificationTime,
  formatUnreadBadge,
  patchAllPagesRead,
  patchPagesRead,
} from '../../src/features/notify/display.ts'

test('buildNotificationSearch defaults limit to 20 and omits empty cursor', () => {
  assert.equal(buildNotificationSearch(), `?limit=${NOTIFICATION_PAGE_LIMIT}`)
  assert.equal(buildNotificationSearch({ cursor: '' }), '?limit=20')
  assert.equal(buildNotificationSearch({ cursor: 'next-1', limit: 10 }), '?limit=10&cursor=next-1')
})

test('INBOX_VIEW_ROLES admits master and admin and excludes user', () => {
  assert.equal(roleAllowed('master', INBOX_VIEW_ROLES), true)
  assert.equal(roleAllowed('admin', INBOX_VIEW_ROLES), true)
  assert.equal(roleAllowed('user', INBOX_VIEW_ROLES), false)
  assert.equal(roleAllowed(null, INBOX_VIEW_ROLES), false)
})

test('categoryMeta maps all four contract categories and provides a fallback', () => {
  assert.equal(categoryMeta('credential_invalid').label, '凭据失效')
  assert.equal(categoryMeta('review_pending').label, '账号待审')
  assert.equal(categoryMeta('sign_failed_daily').label, '失败摘要')
  assert.equal(categoryMeta('capacity_rejected').label, '容量不足')
  assert.equal(categoryMeta('unknown_category').label, '系统通知')
})

test('formatUnreadBadge hides zero/negative and caps above 99', () => {
  assert.equal(formatUnreadBadge(0), null)
  assert.equal(formatUnreadBadge(-2), null)
  assert.equal(formatUnreadBadge(null), null)
  assert.equal(formatUnreadBadge(3), '3')
  assert.equal(formatUnreadBadge(99), '99')
  assert.equal(formatUnreadBadge(120), '99+')
})

test('formatNotificationTime formats Shanghai relative minutes, hours, yesterday and dates', () => {
  const now = new Date('2030-01-01T09:00:00+08:00')
  assert.equal(formatNotificationTime(null, now), '—')
  assert.equal(formatNotificationTime('bad-time', now), 'bad-time')
  assert.equal(formatNotificationTime('2030-01-01T08:59:30+08:00', now), '刚刚')
  assert.equal(formatNotificationTime('2030-01-01T08:55:00+08:00', now), '5 分钟前')
  assert.equal(formatNotificationTime('2030-01-01T06:15:00+08:00', now), '2 小时前')
  assert.equal(formatNotificationTime('2029-12-31T18:20:00+08:00', now), '昨天 18:20')
  assert.equal(formatNotificationTime('2029-12-29T09:00:00+08:00', now), '2029-12-29 09:00')

  const midYearNow = new Date('2030-06-15T10:00:00+08:00')
  assert.equal(formatNotificationTime('2030-06-01T08:05:00+08:00', midYearNow), '06-01 08:05')
})

test('patchPagesRead and patchAllPagesRead update only unread rows and keep prior timestamps', () => {
  const data = {
    pages: [
      {
        items: [
          {
            id: 'n1',
            category: 'credential_invalid',
            title: 't1',
            body: 'b1',
            school_id: 'school-a',
            created_at: '2030-01-01T08:55:00+08:00',
            read_at: null,
          },
          {
            id: 'n2',
            category: 'review_pending',
            title: 't2',
            body: 'b2',
            school_id: null,
            created_at: '2030-01-01T08:40:00+08:00',
            read_at: '2030-01-01T08:45:00+08:00',
          },
        ],
        next_cursor: 'c2',
      },
      {
        items: [
          {
            id: 'n3',
            category: 'capacity_rejected',
            title: 't3',
            body: 'b3',
            school_id: null,
            created_at: '2030-01-01T08:10:00+08:00',
            read_at: null,
          },
        ],
        next_cursor: '',
      },
    ],
    pageParams: ['', 'c2'],
  }

  const single = patchPagesRead(data, ['n1', 'n2'], '2030-01-01T09:00:00+08:00')
  assert.equal(single.pages[0].items[0].read_at, '2030-01-01T09:00:00+08:00')
  assert.equal(single.pages[0].items[1].read_at, '2030-01-01T08:45:00+08:00')
  assert.equal(single.pages[1].items[0].read_at, null)

  const all = patchAllPagesRead(single, '2030-01-01T09:01:00+08:00')
  assert.equal(all.pages[1].items[0].read_at, '2030-01-01T09:01:00+08:00')
  assert.equal(all.pages[0].items[1].read_at, '2030-01-01T08:45:00+08:00')
})

test('flattenUniqueNotifications deduplicates overlapping page boundaries by id', () => {
  const baseItem = {
    id: 'n1',
    category: 'credential_invalid',
    title: 't1',
    body: 'b1',
    school_id: 'school-a',
    created_at: '2030-01-01T08:55:00+08:00',
    read_at: null,
  }
  const merged = flattenUniqueNotifications([
    { items: [baseItem], next_cursor: 'c2' },
    { items: [baseItem, { ...baseItem, id: 'n2' }], next_cursor: '' },
  ])
  assert.deepEqual(
    merged.map((item) => item.id),
    ['n1', 'n2'],
  )
})

test('showcase notification fixtures align unread count with list items', () => {
  const page = showcaseNotifications()
  const unread = showcaseUnreadCount()
  assert.equal(page.items.length, 4)
  assert.equal(unread.count, 2)
  assert.equal(page.items.filter((item) => item.read_at === null).length, unread.count)
})
