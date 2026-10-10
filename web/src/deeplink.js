/** 从地址栏取筛选条件的初值。
 *
 *  写这个文件的直接原因：接口一直支持 `?company=`、`?level=`，而页面只读了
 *  `?search=`。于是驾驶舱上「按公司」那张卡、「未处置预警」那枚磁贴、事件详情页
 *  面包屑里的三条链接，点过去参数都被丢掉——落到的是全量列表，看起来像那条链接
 *  本来就该这样。这类失败不报错，只是安静地给出另一页内容。
 *
 *  只认这一页自己声明过的键：地址栏里多出来的键不该凭空造出一个筛选，因为界面上
 *  没有对应的控件把它清掉。
 */

/** `route.query` 的一个值可能是 string、string[]（`?a=1&a=2`）或 null（`?a`）。
 *
 *  这一步不是防御性的：`CompanySelect` 与 `EnumSelect` 的 `modelValue` 都声明成
 *  String，把数组或 null 灌进去会触发 Vue 的 prop 类型告警，而 `__tests__/setup.js`
 *  把运行时告警变成测试失败。线上则是一行控制台噪音加一个行为古怪的下拉。 */
const first = (value) => {
  const item = Array.isArray(value) ? value[0] : value
  return typeof item === 'string' ? item : ''
}

/** 用 `blank` 的形状开一份新的筛选条件，地址栏里给了值的键取地址栏的。
 *
 *  返回新对象而不是就地改：调用点是 `ref(fromQuery(BLANK, route.query))`，此时
 *  页面上那个 `watch(filters, …, { deep: true })` 还没注册，不会因为初值被改而
 *  多发一次请求。 */
export const fromQuery = (blank, query) => Object.fromEntries(
  Object.entries(blank).map(([key, fallback]) => [key, first(query?.[key]) || fallback]))

/** 地址栏里点名要展开的那一行，前提是它确实在这一页上。
 *
 *  分开写是因为展开与筛选是两件事：筛选错了读者看得见「共 N 条」，展开错了则是
 *  一个永远转不完的加载态——被点名的行不在当前页时，直接把 id 塞进 `expanded`
 *  就是这个下场。 */
export const wanted = (query, key, rows, idKey) => {
  const id = first(query?.[key])
  return id && (rows || []).some((row) => row[idKey] === id) ? id : null
}
