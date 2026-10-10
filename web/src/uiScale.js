/** 界面缩放倍数：根字号 ÷ 16。
 *
 *  样式表里的尺寸在构建期换成了 rem，自己会跟着根字号走；走不到的是在 JS 里
 *  给出像素的两处——组件库的主题（字号、控件高度）与图表 option（字号、图例
 *  色块）。它们从这里读倍数，而不是各自再算一遍视口宽度：根字号的公式只写在
 *  `styles.css` 一处，这里只是把结果读回来。
 */
import { ref } from 'vue'

const ROOT_PX = 16

function read() {
  if (typeof document === 'undefined' || typeof getComputedStyle !== 'function') return 1
  const px = parseFloat(getComputedStyle(document.documentElement).fontSize)
  // 读不到（jsdom、样式表未就绪）就当 1 倍。取两位小数：拖动窗口时根字号是
  // 连续变的，不取整的话每一个像素都会让所有图表重画一次。
  return px > 0 ? Math.round((px / ROOT_PX) * 100) / 100 : 1
}

export const uiScale = ref(read())

if (typeof window !== 'undefined') {
  let pending = false
  const update = () => {
    if (pending) return
    pending = true
    const later = typeof requestAnimationFrame === 'function' ? requestAnimationFrame : setTimeout
    later(() => {
      pending = false
      const next = read()
      if (next !== uiScale.value) uiScale.value = next
    })
  }
  window.addEventListener('resize', update)
  // 模块求值时样式表未必已经挂上：main.js 先引 App 再引 styles.css，开发模式下
  // 那张表是后一条 import 才注入的。第一帧之后再读一次。
  update()
}

/** 把一个以 1 倍为基准的像素数按当前倍数放大，取整。 */
export function scalePx(px, scale = uiScale.value) {
  return Math.round(px * scale)
}
