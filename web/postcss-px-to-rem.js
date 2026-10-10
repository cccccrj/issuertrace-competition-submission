/**
 * 构建期把样式里的 px 换成 rem，让整套界面跟着根字号一起缩放。
 *
 * 为什么是构建期而不是改源码：全站样式里有上千处 px，字号、间距令牌的注释
 * 全是按 px 论证的（「11 / 12 / 13 三级」「相邻两级 ≥1.06」）。把源码改成
 * 0.8125rem 会让那些论证读不懂，测试里钉住的 px 值也要跟着改一遍。源码继续
 * 写 px，浏览器拿到的是 rem——根字号 16px 时两者逐像素相同。
 *
 * 为什么不用 CSS zoom：zoom 挂在 <html> 上之后，getBoundingClientRect 与
 * 定位写回的 left/top 不在同一个坐标系里，组件库的弹层、检索框的下拉会整体
 * 偏出 zoom 倍。rem 只改尺寸，不碰坐标系。
 *
 * 三条例外：
 * - 绝对值小于 2px 的不换。发丝线在 1.25 倍下变成 1.25px，会被糊成半透明的两条。
 * - `html` 规则不换——那是根字号本身，换成 rem 就成了自己引用自己。
 * - 规则里写了 `/* keep-px *\/` 的整条不换。给 SVG 里的文字用：SVG 的字号落在
 *   viewBox 的用户单位里，图整体已经按容器缩放，字号再放大一次就会撑出节点框。
 *
 * 媒体查询的断点写在 at-rule 参数里，不是声明，本插件不碰——断点比的是视口
 * 宽度，本来就该是 px。
 */
const PX = /(-?\d*\.?\d+)px\b/g
const ROOT_PX = 16

function toRem(value) {
  return value.replace(PX, (match, number) => {
    const px = Number(number)
    if (Math.abs(px) < 2) return match
    return `${Number((px / ROOT_PX).toFixed(5))}rem`
  })
}

export default function pxToRem() {
  return {
    postcssPlugin: 'px-to-rem',
    Declaration(decl) {
      if (!decl.value.includes('px')) return
      const rule = decl.parent
      if (rule?.type === 'rule') {
        if (rule.selector.trim() === 'html') return
        if (rule.nodes.some((node) => node.type === 'comment' && node.text.trim() === 'keep-px')) return
      }
      const next = toRem(decl.value)
      if (next !== decl.value) decl.value = next
    },
  }
}
pxToRem.postcss = true

export { toRem }
