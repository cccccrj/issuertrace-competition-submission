/** I-04：报道发布时尚在未来的日期显示为「计划/截止」，不冒充发生时间。 */
import { describe, expect, it } from 'vitest'

import { targetText } from '../../api.js'

describe('targetText', () => {
  it('names a single date as a plan or deadline', () => {
    expect(targetText('2032-01-01')).toBe('计划/截止 2032-01-01')
  })

  it('writes a range out in full rather than as its first day', () => {
    expect(targetText('2027-01-01/2027-12-31')).toBe('计划/截止 2027-01-01 至 2027-12-31')
  })

  it('is empty when there is nothing scheduled', () => {
    expect(targetText(null)).toBe('')
  })
})
