/**
 * 数字入场时从 0 滚到终值——只滚一次，只在首屏。
 *
 * 读者要的是终值；滚动只是让「这个数是刚取来的」被看见。所以它必须短（600ms）、
 * 必须落在准确的终值上（最后一帧直接写终值，不靠浮点累加），并且在
 * `prefers-reduced-motion` 下一帧都不动，直接显示终值。
 */
import { onBeforeUnmount, ref, watch } from 'vue'

/** 读不到偏好（没有 matchMedia 的环境）时按「不要动」处理：宁可少一段动画，
 *  也不在一个没法确认读者意愿的地方动起来。 */
export const reducedMotion = () =>
  typeof window === 'undefined' || typeof window.matchMedia !== 'function'
  || window.matchMedia('(prefers-reduced-motion: reduce)').matches

/**
 * @param source 返回目标数值的 getter；值为 null/undefined 时显示 null。
 * @param duration 毫秒。
 * @returns ref，当前应显示的整数。
 */
export function useCountUp(source, duration = 600) {
  const shown = ref(null)
  let frame = null
  const stop = () => { if (frame) cancelAnimationFrame(frame); frame = null }

  watch(source, (target) => {
    stop()
    if (target === null || target === undefined || Number.isNaN(target)) {
      shown.value = null
      return
    }
    if (reducedMotion() || typeof requestAnimationFrame !== 'function' || target === 0) {
      shown.value = target
      return
    }
    // 起点取第一帧自己的时间戳，而不是 performance.now()：两者不保证是同一个时钟，
    // 混用时进度会算成负数，数字从负几万滚上来。
    let started = null
    const tick = (now) => {
      if (started === null) started = now
      const progress = Math.min(1, Math.max(0, (now - started) / duration))
      const eased = 1 - (1 - progress) ** 3        // ease-out cubic
      shown.value = progress < 1 ? Math.round(target * eased) : target
      frame = progress < 1 ? requestAnimationFrame(tick) : null
    }
    frame = requestAnimationFrame(tick)
  }, { immediate: true })

  onBeforeUnmount(stop)
  return shown
}
