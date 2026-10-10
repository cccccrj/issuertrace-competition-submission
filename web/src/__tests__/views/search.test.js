/** 全局检索：一个不必先猜「这东西属于哪一页」的入口。
 *
 *  这一组测试守三件事：分组照接口给的顺序渲染（不重排——结构化组没有分数，
 *  披露原文是 bm25，两者不可比）；披露原文那一组说「前 N 条」而不是编一个总数；
 *  「查看全部」把词交回给那一页自己的 search 参数，而不是在这里重做一份分页。
 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const calls = []

const GROUPS = [
  { kind: 'event', label: '事件', total: 444, more: { path: '/events', query: { search: '回购' } },
    items: [{ event_id: 'e1', company_id: 'wuxi_apptec', summary: '公司完成 10 亿元回购',
              event_time: '2024-02-05', risk_level: 'low', title: '回购结果公告',
              published_at: '2024-02-05' }] },
  { kind: 'claim', label: '核验点', total: 508, more: { path: '/claims', query: { search: '回购' } },
    items: [{ claim_id: 'c1', event_id: 'e1', text: '累计回购 2027.54 万股',
              issue_code: 'SHAREHOLDER', company_id: 'wuxi_apptec' }] },
  { kind: 'issue', label: '专题', total: 0, more: null, items: [] },
  { kind: 'alert', label: '风险事项', total: 63, more: { path: '/risks', query: { search: '回购' } },
    items: [{ alert_id: 'a1', company_id: 'cxmt', headline: '回购口径变化',
              risk_level: 'high', issue_code: 'FUND_USE' }] },
  { kind: 'diff', label: '披露变化', total: 4, more: { path: '/diffs', query: { search: '回购' } },
    items: [{ diff_id: 'd1', company_id: 'cxmt', section: '第七节', right_text: '回购安排调整',
              left_text: '原表述', metric: '回购金额' }] },
  { kind: 'document', label: '原始文档', total: 179,
    more: { path: '/ingestion', query: { search: '回购' } },
    items: [{ version_id: 'v1', company_id: 'wuxi_apptec', title: '藥明康德回購H股公告',
              published_at: '2026-08-03' }] },
  { kind: 'evidence', label: '披露原文', total: null, more: null,
    items: [{ paragraph_id: 1, company_id: 'cxmt', title: '注册稿_招股说明书', section: '第七节',
              page: 164, text: '本次拟募集资金295亿元。', score: -11.2 }] },
]

const state = { groups: GROUPS, query: { q: '回购' } }

vi.mock('../../api.js', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    api: {
      search: vi.fn(async (params) => {
        calls.push(params)
        return { query: params.q, limit: params.limit, groups: state.groups }
      }),
    },
  }
})

vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    useRoute: () => ({ query: state.query, params: {} }),
    useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  }
})

import SearchView from '../../views/SearchView.vue'

const STUBS = { RouterLink: { props: ['to'], template: '<a :href="typeof to === \'string\' ? to : to.path"><slot /></a>' } }

async function render() {
  const wrapper = mount(SearchView, { global: { stubs: STUBS } })
  await new Promise((resolve) => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

beforeEach(() => {
  calls.length = 0
  state.groups = GROUPS
  state.query = { q: '回购' }
})

describe('全局检索结果页', () => {
  it('从地址栏的词直接开搜，不用再敲一遍', async () => {
    await render()
    expect(calls[0].q).toBe('回购')
    expect(calls[0].limit).toBe(20)
  })

  it('七组按接口给的顺序渲染，空组不占位置', async () => {
    const wrapper = await render()
    const names = wrapper.findAll('.name').map((node) => node.text())
    // 议题组这次没命中，不渲染——空标题比没有标题更糟。
    expect(names).toEqual(['事件', '核验点', '风险事项', '披露变化', '原始文档', '披露原文'])
  })

  it('结构化组报总数，披露原文报「前 N 条」而不是编一个总数', async () => {
    const wrapper = await render()
    const text = wrapper.text()
    expect(text).toContain('共 444 条')
    expect(text).toContain('前 1 条')
  })

  it('「查看全部」把词交回那一页自己的 search 参数', async () => {
    const wrapper = await render()
    const links = wrapper.findAll('a').map((node) => node.attributes('href'))
    expect(links).toContain('/events')
    expect(wrapper.text()).toContain('在该页查看全部 444 条')
  })

  it('披露原文给的是能回去核对的地址：文件、节、页码', async () => {
    const wrapper = await render()
    const text = wrapper.text()
    expect(text).toContain('本次拟募集资金295亿元')
    expect(text).toContain('第164页')
    expect(text).toContain('注册稿_招股说明书')
  })

  /* 2026-09-06 按产品决定移除：页面不再自述口径与方法学，说明性文字整体从站内
     撤下（约 4,100 字），这些话改由技术报告与答辩材料承担。删的是断言，不是
     曾经的判断——原句与钉它的理由见 docs/界面体检记录.md §四。
     2026-09-07 补记：导语按产品决定加回来了，但形态换了——每页一句、不超过 40 字、
     只回答「这一页在回答什么问题」，由 ui/conventions.test.js 的「每页一句导语」钉住；
     方法学与「这不是什么」归 /guide 使用指南页，不再摊回每一页。上面撤下的是后者。 */

  it('一条都没命中时说没有结果，而不是留一片空白', async () => {
    state.groups = GROUPS.map((group) => ({ ...group, items: [], total: 0 }))
    const wrapper = await render()
    expect(wrapper.text()).toContain('没有匹配的结果')
  })

  it('一个字都还没输时不说「没有匹配的结果」——没查过的东西说不上有没有', async () => {
    state.query = {}
    state.groups = []
    const wrapper = await render()
    expect(wrapper.text()).not.toContain('没有匹配的结果')
    expect(wrapper.text()).toContain('输入检索词')
  })
})

/** 「查看全部」的另一半：目标页面得真的认得地址上的 search 参数。
 *
 *  改动之前只有 PropagationView 读 route.query，其余页面一概忽略——链接跳过去
 *  会落在一张没有筛选的全量列表上，等于把人送到门口再让他重新找。
 */
describe('目标页面接得住带过来的词', () => {
  it('/events?search=… 打开时，搜索框里就是那个词，请求也带着它', async () => {
    vi.resetModules()
    const requested = []
    vi.doMock('../../api.js', async (importOriginal) => {
      const actual = await importOriginal()
      return {
        ...actual,
        api: {
          events: vi.fn(async (params) => {
            requested.push(params)
            return { total: 0, limit: 25, offset: 0, items: [] }
          }),
          event: vi.fn(async () => ({})),
        },
      }
    })
    vi.doMock('vue-router', async (importOriginal) => {
      const actual = await importOriginal()
      return { ...actual, useRoute: () => ({ query: { search: '回购' }, params: {} }) }
    })
    const { default: EventsView } = await import('../../views/EventsView.vue')
    const wrapper = mount(EventsView, { global: { stubs: STUBS } })
    await new Promise((resolve) => setTimeout(resolve, 0))
    await wrapper.vm.$nextTick()
    expect(wrapper.find('input[type="search"]').element.value).toBe('回购')
    expect(requested[0].search).toBe('回购')
    vi.doUnmock('../../api.js')
    vi.doUnmock('vue-router')
  })
})
