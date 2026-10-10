import { ref, watch } from 'vue'

/** 让一个文本输入延迟写回筛选条件。
 *
 *  站里十个页面的搜索框都是 `v-model="filters.search"` 加一个
 *  `watch(filters, …, { deep: true })`，也就是**每敲一个字发一次请求**。全局
 *  检索会把这件事放大——它一次要问七张表，其中一路还要走 FTS——所以防抖是这次
 *  必须一起修的东西，而不是顺手加的优化。
 *
 *  只包文本这一路：公司、议题、等级那些离散筛选项是一次点击一个决定，延迟它们
 *  只会让人以为没点上。
 *
 *  250ms 是取舍值，**有论证、无验证**：低于它，按键还在打字中途就发请求；高于
 *  它，下拉显得迟钝。没有对本项目的键入速度做过标定。
 *
 *  中文输入法在上屏前会连续触发 input 事件，这里按普通输入处理，没有监听
 *  compositionstart/end——250ms 的窗口内通常已经选完候选，但这是个未验证的判断。
 */
export function debouncedField(filters, key = 'search', delay = 250) {
  const draft = ref(filters.value[key] ?? '')
  let timer = null
  watch(draft, (value) => {
    clearTimeout(timer)
    timer = setTimeout(() => { filters.value[key] = value }, delay)
  })
  // 「清除筛选」把整个 filters 换成新对象时，输入框要跟着空掉。
  watch(() => filters.value[key], (value) => {
    if ((value ?? '') !== draft.value) draft.value = value ?? ''
  })
  return draft
}
