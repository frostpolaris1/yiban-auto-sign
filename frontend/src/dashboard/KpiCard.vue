<script setup lang="ts">
/* 数据看板 KPI 卡（整卡可点）：与 legacy 模板的 kpi 宏同结构。
   四张卡共用一套渲染，差异（配色 / 图标 / 链接 / 是否带药丸 / 告警强调）全走 props；
   `view` 为 null 表示数据未到（保留骨架），`view.failed` 表示对应接口失败（错误行）。

   ⚠ `showPill` 必须用 withDefaults 给默认 true：Vue 对**声明为 Boolean 的 prop** 做
   布尔转换——缺省时会被当成 false，`v-if="showPill !== false"` 就永远不成立，药丸全灭。 */
withDefaults(defineProps<{
  k: string;
  color: string;
  icon: string;
  label: string;
  href: string;
  view: any;
  showPill?: boolean;
}>(), { showPill: true });
</script>

<template>
  <a class="kpi-card" :class="['c-' + color, view && view.alert ? 'is-alert' : '']" :href="href">
    <div class="kpi-top">
      <div class="kpi-identity">
        <div class="kpi-icon" :class="color"><svg aria-hidden="true"><use :href="'#i-' + icon" /></svg></div>
        <div class="kpi-label">{{ label }}</div>
      </div>
      <span
        v-if="showPill !== false"
        class="kpi-pill"
        :class="view && view.pill ? view.pill.cls : ''"
        :id="'kpi-' + k + '-pill'"
        :title="view && view.pill ? view.pill.title : ''"
        :hidden="!(view && view.pill && view.pill.text)"
      >{{ view && view.pill ? view.pill.text : "" }}</span>
    </div>
    <div class="kpi-value" :class="{ 'kpi-value--empty': view && view.empty }" :id="'kpi-' + k + '-value'" :title="view ? view.valueTitle : ''">
      <span v-if="!view" class="skeleton skeleton--title dash-skel" aria-hidden="true"></span>
      <span v-else-if="view.failed" class="dash-error-text">—</span>
      <template v-else>{{ view.value }}<sup v-if="view.sup != null">{{ view.sup }}</sup></template>
    </div>
    <div class="kpi-compare" :id="'kpi-' + k + '-sub'" :title="view ? view.subTitle : ''">
      <span v-if="!view" class="skeleton skeleton--text dash-skel" aria-hidden="true"></span>
      <span v-else-if="view.failed" class="dash-error-text">{{ view.sub }}</span>
      <span v-else class="kpi-compare-text" :class="view.subCls">{{ view.sub }}</span>
    </div>
  </a>
</template>
