<script setup>
/** 下拉选择——自绘，不是原生 `<select>`。
 *
 *  为什么换掉：原生下拉展开的那张列表由操作系统绘制，CSS 一点也够不着。在这套
 *  对表面和发丝线如此讲究的界面里，它是唯一一块看起来来自别处的东西，也是
 *  [界面体检记录](../../../../docs/界面体检记录.md) 第 10 条留下的最后一项。
 *
 *  **换掉它是有代价的，这个组件存在的大半理由是把代价还回去。** 原生
 *  `<select>` 白送键盘操作、读屏支持和首字母跳转；换成 div 拼的控件，这些默认
 *  全部消失，只剩好看。所以这里逐项补回：
 *
 *  - Enter / Space / ↓ 展开，Esc 关闭并把焦点还给按钮，Tab 关闭；
 *  - ↑↓ 移动高亮，Home / End 跳到首尾，Enter / Space 选中；
 *  - 键入字符按标签前缀跳转，1 秒无输入后重置——议题码有 26 个，没有这条就
 *    只能一格一格按下去；
 *  - `aria-haspopup` / `aria-expanded` / `role=listbox` / `role=option` /
 *    `aria-selected`，以及 `aria-activedescendant`：高亮项要让读屏念得出来，
 *    而键盘焦点自始至终停在按钮上。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  modelValue: { type: [String, null], default: '' },
  /** [{ value, label, count?, disabled? }]，顺序即呈现顺序。
   *  `disabled` 的项照常列出但选不中，也不会被方向键停留——只列不给选，
   *  和干脆不列是两件事：前者说明「有这个选项，但你不能选」。 */
  options: { type: Array, required: true },
  /** 无障碍名称。下拉没有可见标题时，这是读屏唯一能报出的东西。 */
  label: { type: String, required: true },
  disabled: { type: Boolean, default: false },
  /** `filter` 是筛选栏里的方角控件；`pill` 是浮在角上的圆角胶囊。只有两种，
   *  因为界面上只有两处需要它长得不一样。 */
  variant: { type: String, default: 'filter' },
  /** 贴右边缘时菜单要向左展开，否则会被视口切掉。 */
  align: { type: String, default: 'left' },
  /** 贴下边缘时菜单要向上展开。与 `align` 同一个道理，只是换一根轴：控件落在
   *  侧栏最底下时向下弹会盖住它下面的账号行，再往下还会被侧栏的滚动区切掉。 */
  drop: { type: String, default: 'down' },
})
const emit = defineEmits(['update:modelValue'])

const open = ref(false)
const box = ref(null)
const button = ref(null)
const listbox = ref(null)
const activeIndex = ref(-1)
const uid = `sel-${Math.random().toString(36).slice(2, 9)}`

const selectedIndex = computed(() =>
  props.options.findIndex((option) => option.value === props.modelValue))
const current = computed(() => props.options[selectedIndex.value] || props.options[0] || null)

function show() {
  if (props.disabled) return
  open.value = true
  // 展开时高亮当前值，而不是从头开始：一个 26 项的表里，从头开始意味着每次都要
  // 重新走一遍才回到原处。
  const start = selectedIndex.value >= 0 ? selectedIndex.value : 0
  activeIndex.value = props.options[start]?.disabled
    ? props.options.findIndex((option) => !option.disabled)
    : start
  nextTick(scrollActiveIntoView)
}

function hide(returnFocus = true) {
  open.value = false
  activeIndex.value = -1
  if (returnFocus) button.value?.focus()
}

function choose(index) {
  const option = props.options[index]
  if (!option || option.disabled) return
  emit('update:modelValue', option.value)
  hide()
}

function move(delta) {
  const total = props.options.length
  if (!total) return
  // 跳过禁用项，最多绕一圈：整列都禁用时停在原地而不是死循环。
  let next = activeIndex.value
  for (let step = 0; step < total; step += 1) {
    next = (next + delta + total) % total
    if (!props.options[next].disabled) { activeIndex.value = next; break }
  }
  nextTick(scrollActiveIntoView)
}

function scrollActiveIntoView() {
  listbox.value?.querySelector('[data-active="true"]')?.scrollIntoView({ block: 'nearest' })
}

/** 首字母跳转。原生下拉自带，去掉就是净损失。 */
let typed = ''
let typedAt = 0
function jumpTo(character) {
  const now = Date.now()
  typed = now - typedAt > 1000 ? character : typed + character
  typedAt = now
  const found = props.options.findIndex((option) =>
    !option.disabled && String(option.label).toLowerCase().startsWith(typed.toLowerCase()))
  if (found >= 0) {
    activeIndex.value = found
    nextTick(scrollActiveIntoView)
  }
}

function onKeydown(event) {
  if (props.disabled) return
  const { key } = event
  if (!open.value) {
    if (key === 'Enter' || key === ' ' || key === 'ArrowDown' || key === 'ArrowUp') {
      event.preventDefault()
      show()
    }
    return
  }
  if (key === 'Escape') { event.preventDefault(); hide(); return }
  if (key === 'Tab') { hide(false); return }
  if (key === 'ArrowDown') { event.preventDefault(); move(1); return }
  if (key === 'ArrowUp') { event.preventDefault(); move(-1); return }
  if (key === 'Home') { event.preventDefault(); activeIndex.value = -1; move(1); return }
  if (key === 'End') { event.preventDefault(); activeIndex.value = props.options.length; move(-1); return }
  if (key === 'Enter' || key === ' ') { event.preventDefault(); choose(activeIndex.value); return }
  if (key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey) jumpTo(key)
}

function onOutside(event) {
  if (open.value && box.value && !box.value.contains(event.target)) hide(false)
}

/** Escape 从任何地方都该关掉一个浮层，不只是焦点还在按钮上的时候。
 *  按钮上的 keydown 覆盖不到这一条：点开之后焦点可能已经被别处拿走。 */
function onGlobalEscape(event) {
  if (open.value && event.key === 'Escape') hide(false)
}

onMounted(() => {
  document.addEventListener('click', onOutside)
  document.addEventListener('keydown', onGlobalEscape)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onOutside)
  document.removeEventListener('keydown', onGlobalEscape)
})
// 选项换了一批（换了公司、改了筛选）时，展开中的高亮位置不再指向同一样东西。
watch(() => props.options, () => { if (open.value) activeIndex.value = 0 })
</script>

<template>
  <div ref="box" class="select" :class="[variant, align, { open, disabled, 'drop-up': drop === 'up' }]">
    <button
      ref="button" type="button" class="control"
      :disabled="disabled" :aria-label="label"
      aria-haspopup="listbox" :aria-expanded="open"
      :aria-controls="open ? uid : undefined"
      :aria-activedescendant="open && activeIndex >= 0 ? `${uid}-${activeIndex}` : undefined"
      @click="open ? hide() : show()" @keydown="onKeydown"
    >
      <span class="value">{{ current ? current.label : '' }}</span>
      <span class="chev" aria-hidden="true">▾</span>
    </button>
    <ul v-if="open" :id="uid" ref="listbox" class="menu" role="listbox" :aria-label="label">
      <li
        v-for="(option, index) in options" :id="`${uid}-${index}`" :key="option.value"
        role="option" :aria-selected="option.value === modelValue"
        :aria-disabled="option.disabled || undefined"
        :data-active="index === activeIndex"
        class="option" :class="{ active: index === activeIndex, off: option.disabled }"
        @click="choose(index)" @mousemove="option.disabled || (activeIndex = index)"
      >
        <span class="tick" aria-hidden="true">{{ option.value === modelValue ? '✓' : '' }}</span>
        <span class="text">{{ option.label }}</span>
        <span v-if="option.count != null" class="count mono">{{ option.count }}</span>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.select { position: relative; display: inline-block; }
.pill .control { border-radius: 999px; padding: 5px 11px; border-color: var(--border);
  color: var(--text-secondary); }
.pill .menu { border-radius: 10px; padding: 4px;
  box-shadow: 0 1px 2px rgba(11, 11, 11, 0.05), 0 10px 28px -10px rgba(11, 11, 11, 0.22); }
.pill .option { padding: 6px 10px; border-radius: 6px; }
.right .menu { left: auto; right: 0; }
.drop-up .menu { top: auto; bottom: calc(100% + 3px); }
.control { display: flex; align-items: center; gap: 6px; width: 100%; font: inherit;
  font-size: var(--fs-aux); padding: 4px 8px; color: var(--text-primary);
  background: var(--surface-1); border: 1px solid var(--gridline); border-radius: 4px;
  cursor: pointer; transition: border-color 0.14s ease; }
.control:hover:not(:disabled) { border-color: var(--text-muted); }
.control:focus-visible { outline: 2px solid var(--series-1); outline-offset: 1px; }
.control:disabled { cursor: default; color: var(--text-muted); }
.value { flex: 1; text-align: left; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.chev { flex: none; font-size: 9px; color: var(--text-muted); }
.open .chev { transform: rotate(180deg); }
.menu { position: absolute; z-index: 40; top: calc(100% + 3px); left: 0; min-width: 100%;
  max-height: 280px; overflow-y: auto; margin: 0; padding: 3px; list-style: none;
  background: var(--surface-1); border: 1px solid var(--gridline); border-radius: 5px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.12); }
.option { display: flex; align-items: baseline; gap: 6px; padding: 4px 7px; font-size: var(--fs-aux);
  color: var(--text-secondary); border-radius: 3px; cursor: pointer; white-space: nowrap; }
/* 高亮只跟键盘和指针走，不表示选中——选中由 ✓ 和 aria-selected 承担，
   两件事挤在一种视觉里，读屏和肉眼会看到不同的答案。 */
.option.active { background: var(--hover-wash); color: var(--text-primary); }
.option.off { color: var(--text-muted); cursor: default; }
.option.off:hover { background: none; }
.tick { flex: none; width: 1em; color: var(--series-1); }
.count { flex: none; margin-left: auto; padding-left: 10px; color: var(--text-muted); font-size: var(--fs-note); }
</style>
