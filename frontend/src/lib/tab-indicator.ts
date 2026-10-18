import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch, type Ref } from "vue";

/**
 * 自研页签的滑动指示条。
 *
 * 背景：账号/用户/设置三页的页签是手写 `<a class="tab">`，选中态靠 `border-bottom`
 * 直接跳变；日志页用的 Element Plus 页签有一条会滑动的 active bar。用户要求
 * "日志页是做得比较好的，给其他页面补动效"——这里把同一种滑动反馈补到自研页签。
 *
 * 不改成 el-tabs 的原因：这三页的页签还带 roving tabindex、计数、脏分区圆点与
 * 可横向滚动容器，迁移的改动面远大于补一条指示条。
 *
 * 约束：
 * - 只读 DOM 几何（offsetLeft/offsetWidth），不改变页签语义；
 * - `prefers-reduced-motion` 下由 CSS 关掉过渡；
 * - 首次挂载只定位、不播放入场（避免从 0 扫到选中项）。
 */
export function useTabIndicator<K extends string>(root: Ref<HTMLElement | null>, active: Ref<K>) {
  const left = ref(0);
  const width = ref(0);
  const ready = ref(false);
  let resizeObserver: ResizeObserver | null = null;
  let mutationObserver: MutationObserver | null = null;

  function measure(): void {
    const el = root.value?.querySelector<HTMLElement>(".tab.is-active");
    if (!el) {
      width.value = 0;
      return;
    }
    left.value = el.offsetLeft;
    width.value = el.offsetWidth;
  }

  function enable(): void {
    if (ready.value) return;
    measure();
    ready.value = true;
  }

  function onPointerDown(): void {
    enable();
  }

  function onKeyDown(): void {
    enable();
  }

  const style = computed(() => ({
    transform: `translateX(${left.value}px)`,
    width: `${width.value}px`,
  }));

  onMounted(() => {
    void nextTick(measure);
    if (typeof ResizeObserver !== "undefined" && root.value) {
      resizeObserver = new ResizeObserver(measure);
      resizeObserver.observe(root.value);
    }
    // 页签上的计数/徽标是异步填的（先渲染空 span，再填数字），会改变 active 页签宽度；
    // 只观察容器宽度抓不到。MutationObserver 覆盖文本/子节点变化，避免指示条停在旧宽度。
    if (typeof MutationObserver !== "undefined" && root.value) {
      mutationObserver = new MutationObserver(() => measure());
      mutationObserver.observe(root.value, { childList: true, subtree: true, characterData: true });
    }
    root.value?.addEventListener("pointerdown", onPointerDown, { capture: true });
    root.value?.addEventListener("keydown", onKeyDown, { capture: true });
    // 首次交互前不显示指示条（静态下划线就在），首次 pointerdown/keydown 时接管；
    // 这样既避免首屏从 0 扫到选中项，也避免计数异步填充后的一次加载动画。
    window.addEventListener("resize", measure);
    // 字体加载会改变页签宽度；加载完成后重测一次。
    if (typeof document !== "undefined" && document.fonts?.ready) {
      void document.fonts.ready.then(measure);
    }
  });

  watch(active, () => {
    void nextTick(measure);
  });

  onBeforeUnmount(() => {
    resizeObserver?.disconnect();
    resizeObserver = null;
    mutationObserver?.disconnect();
    mutationObserver = null;
    root.value?.removeEventListener("pointerdown", onPointerDown, { capture: true });
    root.value?.removeEventListener("keydown", onKeyDown, { capture: true });
    window.removeEventListener("resize", measure);
  });

  return { style, ready, measure };
}
