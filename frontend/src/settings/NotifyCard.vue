<script setup lang="ts">
import { computed, ref } from "vue";
import { api, errorMessage, toast } from "../lib/shell";
import {
  collectSmtps,
  driftPlaceholder,
  mailBody,
  mailSnapshot,
  mailStatusText,
  newSmtpId,
  notifyBody,
  notifySnapshot,
  notifyStatusText,
  smtpRowsFrom,
} from "./model.js";

/* 通知通道分区：消息推送卡 + 邮件通知卡（各一个保存动作，仅主管理员）。
   legacy = components/settings-notify.js + settings-mail.js。
   读与写不同档：GET 通道配置对任意管理员可读；写与测试仅主管理员且受门禁。
   脱敏：密钥只读展示 secret_masked，输入框恒为空（留空=不改动），绝不回显；SMTP 的 user/pass
   已由后端打码，只作 placeholder，绝不回填 value。 */

const props = defineProps<{ isMaster: boolean }>();

const pushData = ref<Record<string, unknown>>({});
const mailData = ref<Record<string, unknown>>({});
const pushSnap = ref(notifySnapshot({}));
const mailSnap = ref(mailSnapshot({}));
const pushForm = ref({ type: "", secret: "", urgentOnly: false, cooldown: "", dailyMax: "", urgentMax: "" });
const mailForm = ref({ enabled: false, adminTo: "", smtps: [] as Array<Record<string, unknown>> });
const pushDirty = ref(false);
const mailDirty = ref(false);
const tableDirty = ref(false);
const pushTip = ref<{ text: string; bad: boolean }>({ text: "", bad: false });
const mailTip = ref<{ text: string; bad: boolean }>({ text: "", bad: false });
const loading = ref(false);

const typeItems = [
  { value: "", label: "关闭" },
  { value: "serverchan", label: "Server酱（微信 / 方糖服务号）" },
  { value: "custom", label: "自定义 Webhook 地址" },
];

const pushStatus = computed(() => notifyStatusText(pushData.value));
const mailStatus = computed(() => mailStatusText(mailData.value));
const hasTo = computed(() => !!mailSnap.value.hasTo);
const dirty = computed(() => pushDirty.value || mailDirty.value);

async function load(): Promise<void> {
  if (!props.isMaster) return;
  loading.value = true;
  try {
    const [n, m] = await Promise.all([
      api<Record<string, unknown>>("GET", "/api/notify-config"),
      api<Record<string, unknown>>("GET", "/api/mail-config"),
    ]);
    applyNotify(n || {});
    applyMail(m || {});
  } catch (e) {
    pushTip.value = { text: errorMessage(e, "消息推送配置加载失败，请稍后重试"), bad: true };
  } finally {
    loading.value = false;
  }
}
function applyNotify(data: Record<string, unknown>): void {
  pushData.value = data;
  pushSnap.value = notifySnapshot(data);
  pushForm.value = {
    type: pushSnap.value.type,
    secret: "",
    urgentOnly: pushSnap.value.urgent_only,
    cooldown: pushSnap.value.cooldown != null ? String(pushSnap.value.cooldown) : "",
    dailyMax: pushSnap.value.daily_max != null ? String(pushSnap.value.daily_max) : "",
    urgentMax: pushSnap.value.urgent_daily_max != null ? String(pushSnap.value.urgent_daily_max) : "",
  };
  pushDirty.value = false;
  pushTip.value = { text: "", bad: false };
}
function applyMail(data: Record<string, unknown>): void {
  mailData.value = data;
  mailSnap.value = mailSnapshot(data);
  mailForm.value = {
    enabled: mailSnap.value.enabled,
    adminTo: "",
    smtps: smtpRowsFrom((data.smtps as Array<Record<string, unknown>>) || []),
  };
  tableDirty.value = false;
  mailDirty.value = false;
  mailTip.value = { text: "", bad: false };
}

function onPushChange(): void {
  pushDirty.value = Object.keys(notifyBody(pushSnap.value, pushForm.value)).length > 0;
}
function onMailChange(): void {
  mailDirty.value = Object.keys(mailBody(mailSnap.value, mailForm.value, tableDirty.value)).length > 0;
}
function onTableInput(): void {
  tableDirty.value = true;
  onMailChange();
}
function addSmtp(): void {
  if (!props.isMaster) return;
  mailForm.value.smtps.push({ id: newSmtpId(), host: "", port: 465, user0: "留空沿用", has_pass: false, user: "", pass: "" });
  tableDirty.value = true;
  onMailChange();
}
function removeSmtp(i: number): void {
  if (!props.isMaster) return;
  mailForm.value.smtps.splice(i, 1);
  tableDirty.value = true;
  onMailChange();
}
function placeholders(row: Record<string, unknown>): { pass: string; user: string } {
  return driftPlaceholder(row, row.host, row.port);
}

function ops(): Record<string, (c: unknown, a?: unknown) => Promise<boolean>> | undefined {
  return (window as { YB?: { settingsOps?: Record<string, (c: unknown, a?: unknown) => Promise<boolean>> } }).YB?.settingsOps;
}
function pushCtx(): Record<string, unknown> {
  return {
    isMaster: props.isMaster,
    load: async () => { await load(); },
    tip: (t: string, b: boolean) => (pushTip.value = { text: t, bad: b }),
  };
}
function mailCtx(): Record<string, unknown> {
  return {
    isMaster: props.isMaster,
    load: async () => { await load(); },
    tip: (t: string, b: boolean) => (mailTip.value = { text: t, bad: b }),
    afterMailSave: () => { mailForm.value.adminTo = ""; },
  };
}

async function savePush(): Promise<boolean> {
  const o = ops();
  if (!o) return false;
  const body = notifyBody(pushSnap.value, pushForm.value);
  if (!Object.keys(body).length) {
    toast().info("没有需要保存的改动");
    return true;
  }
  if (body.type && !body.secret && !pushSnap.value.configured) {
    toast().error("开启推送请填写密钥");
    return false;
  }
  if (body.secret && body.type === "serverchan" && String(body.secret).slice(0, 3).toUpperCase() !== "SCT") {
    toast().error("Server酱 SendKey 应以 SCT 开头");
    return false;
  }
  const ok = await o.notifySave(pushCtx(), body);
  return ok;
}
async function saveMail(): Promise<boolean> {
  const o = ops();
  if (!o) return false;
  const body = mailBody(mailSnap.value, mailForm.value, tableDirty.value);
  if (!Object.keys(body).length) {
    toast().info("没有需要保存的改动");
    return true;
  }
  if (Object.prototype.hasOwnProperty.call(body, "smtps")) {
    const entries = collectSmtps(mailForm.value.smtps);
    if (entries.some((e) => !e.host)) {
      toast().error("每条 SMTP 都必须填写服务器 host");
      return false;
    }
  }
  return o.mailSave(mailCtx(), body);
}
async function save(): Promise<boolean> {
  let ok = true;
  if (pushDirty.value) ok = await savePush();
  if (ok && mailDirty.value) ok = await saveMail();
  return ok;
}
function testPush(): void {
  const o = ops();
  if (!o) return;
  void o.notifyTest(pushCtx());
}
function clearMail(): void {
  const o = ops();
  if (!o) return;
  void o.mailClear(mailCtx());
}

defineExpose({
  isDirty: () => dirty.value,
  isPushDirty: () => pushDirty.value,
  isMailDirty: () => mailDirty.value,
  save,
  savePush,
  saveMail,
  load,
});
</script>

<template>
  <div id="set-notify" class="set-cards">
    <p class="alert info" id="sn-perm" :hidden="isMaster">
      <span class="ico"><svg aria-hidden="true"><use href="#i-info" /></svg></span>
      <span class="body">仅主管理员可配置消息推送与邮件通道，受限控件已禁用；需调整请联系主管理员。</span>
    </p>

    <section class="card">
      <div class="panel-head">
        <div class="panel-head-row">
          <h2 class="panel-title">消息推送</h2>
          <div class="set-head-actions">
            <button type="button" class="info-tip" aria-label="通知通道说明" aria-describedby="set-pop-notify">
              <svg aria-hidden="true"><use href="#i-info" /></svg>
              <span class="info-pop" id="set-pop-notify" role="tooltip"><b>密钥：</b>加密存储、不回显；Server酱 SendKey 以 SCT 开头，自定义地址须 HTTPS 且非内网。<b>频控：</b>同类告警在冷却时间内只推一条。<b>额度：</b>按「非紧急 / 紧急」两本账各自计数。<b>权限：</b>仅主管理员可修改。<b>确认：</b>关闭通道、更换/清空密钥、调整额度节流均需当前管理员密码。</span>
            </button>
            <span class="badge badge--warn" id="sn-dirty" :hidden="!pushDirty">有未保存的修改</span>
          </div>
        </div>
      </div>
      <div class="form-grid">
        <div class="field">
          <span class="field-label" id="sn-type-label">推送渠道</span>
          <div class="select-field" data-select-field="sn-type">
            <el-select id="sn-type" v-model="pushForm.type" aria-label="推送渠道" :disabled="!isMaster" style="width: 100%" @change="onPushChange">
              <el-option v-for="it in typeItems" :key="it.value" :value="it.value" :label="it.label" />
            </el-select>
          </div>
        </div>
        <div class="field">
          <label class="field-label" for="sn-secret">密钥（留空 = 不改动）</label>
          <input id="sn-secret" v-model="pushForm.secret" class="input" type="password" autocomplete="off" placeholder="SCT 开头的 SendKey 或自定义 https 地址" :disabled="!isMaster" @input="onPushChange" />
          <p class="field-help">加密存储、不回显；自定义地址须 HTTPS 且非内网。</p>
        </div>
      </div>
      <div class="set-row">
        <label class="set-row-text" for="sn-urgent">
          <span class="set-row-label">仅推送重要告警</span>
          <p class="set-help">只推安全与系统级告警；日常类仅走邮件。</p>
        </label>
        <el-switch id="sn-urgent" v-model="pushForm.urgentOnly" aria-label="仅推送重要告警" :disabled="!isMaster" @change="onPushChange" />
      </div>
      <details class="set-more">
        <summary>额度与节流（低频）</summary>
        <div class="form-grid form-grid--3">
          <div class="field">
            <label class="field-label" for="sn-cooldown">同类冷却</label>
            <div class="input-group">
              <input id="sn-cooldown" v-model="pushForm.cooldown" class="input" type="number" min="0" step="1" :disabled="!isMaster" @input="onPushChange" />
              <span class="addon">秒</span>
            </div>
          </div>
          <div class="field">
            <label class="field-label" for="sn-daily-max">非紧急每日上限</label>
            <div class="input-group">
              <input id="sn-daily-max" v-model="pushForm.dailyMax" class="input" type="number" min="0" step="1" :disabled="!isMaster" @input="onPushChange" />
              <span class="addon">条</span>
            </div>
          </div>
          <div class="field">
            <label class="field-label" for="sn-urgent-max">紧急每日上限</label>
            <div class="input-group">
              <input id="sn-urgent-max" v-model="pushForm.urgentMax" class="input" type="number" min="0" step="1" :disabled="!isMaster" @input="onPushChange" />
              <span class="addon">条</span>
            </div>
          </div>
        </div>
      </details>
      <p class="field-help" id="sn-status">{{ isMaster ? pushStatus : "仅主管理员可配置消息推送" }}</p>
      <p class="set-tip" :class="{ 'set-bad': pushTip.bad }" id="sn-tip" role="status">{{ pushTip.text }}</p>
      <div class="form-actions">
        <button type="button" class="btn btn--primary btn--sm" id="sn-save" :hidden="!pushDirty" @click="savePush()">保存推送配置</button>
        <button type="button" class="btn btn--ghost btn--sm" id="sn-test" :disabled="!isMaster" @click="testPush">发送测试消息</button>
      </div>
    </section>

    <section class="card">
      <div class="panel-head">
        <div class="panel-head-row">
          <h2 class="panel-title">邮件通知</h2>
          <div class="set-head-actions">
            <span class="badge badge--warn" id="sm-dirty" :hidden="!mailDirty">有未保存的修改</span>
          </div>
        </div>
      </div>
      <div class="set-row">
        <label class="set-row-text" for="sm-global">
          <span class="set-row-label">全局邮件通知</span>
          <p class="set-help">关闭后所有邮件都不发送（消息推送不受影响）。关闭需当前管理员密码。</p>
        </label>
        <el-switch id="sm-global" v-model="mailForm.enabled" aria-label="全局邮件通知" :disabled="!isMaster" @change="onMailChange" />
      </div>
      <div class="field">
        <label class="field-label" for="sm-to">告警收件人</label>
        <div class="input-group">
          <input id="sm-to" v-model="mailForm.adminTo" class="input" type="text" maxlength="320" autocomplete="off" :placeholder="String(mailData.admin_to || 'admin@example.com')" :disabled="!isMaster" @input="onMailChange" />
          <span class="addon">邮箱</span>
        </div>
        <p class="field-help">多个用英文逗号分隔；留空 = 不改动，清空走下方「清空收件人」。</p>
      </div>

      <p class="field-help" id="sm-status">{{ mailStatus }}</p>
      <div class="data-toolbar">
        <div class="data-toolbar-left">
          <span class="field-label">发件 SMTP（按顺序主备切换，留空 = 沿用旧值）</span>
        </div>
        <div class="data-toolbar-right">
          <button type="button" class="btn btn--ghost btn--sm" id="sm-add-smtp" :disabled="!isMaster" @click="addSmtp">添加备用 SMTP</button>
        </div>
      </div>
      <div class="table-scroll">
        <table class="data-table" id="sm-smtps">
          <thead>
            <tr>
              <th scope="col">服务器 host</th>
              <th scope="col" class="num">端口</th>
              <th scope="col">发件账号</th>
              <th scope="col">授权码</th>
              <th scope="col"><span class="sr-only">操作</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!mailForm.smtps.length" class="sm-empty-row">
              <td colspan="5"><p class="field-help">尚未配置发件 SMTP；添加后告警邮件才可送达。</p></td>
            </tr>
            <tr v-for="(row, i) in mailForm.smtps" :key="String(row.id)" class="sm-row" :data-smtp-id="String(row.id)">
              <td data-label="服务器 host">
                <input v-model="row.host" class="input" type="text" autocomplete="off" placeholder="smtp.example.com" :aria-label="'SMTP ' + (i + 1) + ' 服务器 host'" :disabled="!isMaster" @input="onTableInput" />
              </td>
              <td class="num" data-label="端口">
                <input v-model="row.port" class="input" type="number" min="1" max="65535" placeholder="465" :aria-label="'SMTP ' + (i + 1) + ' 端口'" :disabled="!isMaster" @input="onTableInput" />
              </td>
              <td data-label="发件账号">
                <input v-model="row.user" class="input" type="text" autocomplete="off" :placeholder="placeholders(row).user" :aria-label="'SMTP ' + (i + 1) + ' 发件账号'" :disabled="!isMaster" @input="onTableInput" />
              </td>
              <td data-label="授权码">
                <input v-model="row.pass" class="input" type="password" autocomplete="new-password" :placeholder="placeholders(row).pass" :aria-label="'SMTP ' + (i + 1) + ' 授权码'" :disabled="!isMaster" @input="onTableInput" />
              </td>
              <td class="sm-ops">
                <button type="button" class="btn btn--ghost btn--sm btn--danger-ghost" :aria-label="'删除 SMTP ' + (i + 1)" :disabled="!isMaster" @click="removeSmtp(i)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="set-tip" :class="{ 'set-bad': mailTip.bad }" id="sm-tip" role="status">{{ mailTip.text }}</p>
      <div class="form-actions">
        <button type="button" class="btn btn--primary btn--sm" id="sm-save" :hidden="!mailDirty" @click="saveMail()">保存邮件配置</button>
        <span class="spacer" />
        <button type="button" class="btn btn--ghost btn--sm" id="sm-to-clear" :hidden="!hasTo || !isMaster" @click="clearMail">清空收件人</button>
      </div>
    </section>
  </div>
</template>
