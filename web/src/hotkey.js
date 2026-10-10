/** 键盘快捷键的共用守卫。
 *
 *  这段判断原本长在 `views/ReviewView.vue` 里，服务复核台的 `1/2/3`。全局检索的
 *  `/` 需要同一件事——判断这一次按键属于某个控件还是属于页面——重写一份的唯一
 *  结果是漏掉其中一种控件，所以搬到这里由两处共用，行为逐字节不变。
 */
const FIELDS = ['INPUT', 'SELECT', 'TEXTAREA']

/** Whether the keystroke belongs to a control rather than to the page.
 *
 *  `tagName` alone was the whole test, which is correct only for as long as
 *  every control on the page is a native element. A contenteditable, or a
 *  select rendered as a div, would have typed a `1` straight into `judge()`.
 */
export function isTyping(target) {
  if (!target || FIELDS.includes(target.tagName) || target.isContentEditable) return true
  return Boolean(target.closest?.('[role="combobox"], [role="textbox"], [contenteditable="true"]'))
}
