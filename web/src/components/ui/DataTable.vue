<script setup>
/**
 * The table eleven views wrote out by hand, and the expandable detail row five
 * of them reimplemented independently.
 *
 * It wraps the existing markup rather than adopting a data grid. `styles.css`
 * already styles `table/th/td` at the density this application wants, the
 * report page depends on that markup printing, and swapping the renderer would
 * add weight without answering any question the interface actually has.
 *
 * The expand row gains the one thing every hand-rolled version lacked: an
 * affordance. Today a clickable row looks exactly like an unclickable one, so a
 * reader has to discover by accident that a claim opens.
 */
import { computed } from 'vue'

const props = defineProps({
  /** [{ key, title, width?, align?, nowrap?, slot? }] — `slot` names the scoped
   *  slot rendering the cell; without it the raw value is printed. */
  columns: { type: Array, required: true },
  rows: { type: Array, required: true },
  /** Row identity: a field name, or a function. */
  rowKey: { type: [String, Function], default: 'id' },
  /** The key of the currently open row, or null. One at a time, as every
   *  hand-rolled version did. */
  expanded: { type: [String, Number], default: null },
  /** Present when rows expand; drives the chevron column. */
  expandable: { type: Boolean, default: false },
})

const emit = defineEmits(['update:expanded', 'row-click'])

const keyOf = (row, index) =>
  (typeof props.rowKey === 'function' ? props.rowKey(row, index) : row[props.rowKey] ?? index)

const span = computed(() => props.columns.length + (props.expandable ? 1 : 0))

function activate(row, index) {
  const key = keyOf(row, index)
  emit('row-click', row)
  if (!props.expandable) return
  emit('update:expanded', props.expanded === key ? null : key)
}
</script>

<template>
  <table>
    <thead>
      <tr>
        <!-- 表头此前只写给读屏（`<span class="sr">展开</span>`），视力读者看到的
             只有一枚灰色小箭头，于是「这一行还能点开看具体是哪些」这件事在屏幕上
             没有任何交代。「同源那个只能看到有多少个，不能看到具体哪些」正是这么
             来的——成员表一直都在展开行里。 -->
        <th v-if="expandable" class="chevron" scope="col">展开</th>
        <th
          v-for="column in columns"
          :key="column.key"
          scope="col"
          :style="column.width ? { width: column.width } : null"
          :class="{ right: column.align === 'right' }"
        >{{ column.title }}</th>
      </tr>
    </thead>
    <tbody>
      <template v-for="(row, index) in rows" :key="keyOf(row, index)">
        <tr
          :class="{ clickable: expandable, open: expandable && expanded === keyOf(row, index) }"
          :tabindex="expandable ? 0 : null"
          :aria-expanded="expandable ? String(expanded === keyOf(row, index)) : null"
          @click="activate(row, index)"
          @keydown.enter.prevent="activate(row, index)"
          @keydown.space.prevent="activate(row, index)"
        >
          <td v-if="expandable" class="chevron" aria-hidden="true">
            {{ expanded === keyOf(row, index) ? '▾' : '▸' }}
          </td>
          <td
            v-for="column in columns"
            :key="column.key"
            :class="{ nowrap: column.nowrap, right: column.align === 'right' }"
          >
            <slot v-if="column.slot" :name="column.slot" :row="row" :index="index" />
            <template v-else>{{ row[column.key] }}</template>
          </td>
        </tr>
        <tr v-if="expandable && expanded === keyOf(row, index)" class="detail">
          <td :colspan="span">
            <slot name="expand" :row="row" :index="index" />
          </td>
        </tr>
      </template>
    </tbody>
  </table>
</template>

<style scoped>
.chevron { width: 40px; color: var(--text-muted); padding-right: 0; font-weight: 400; }
/* 箭头在行悬停或展开时变深：静态时它是提示，手放上去时它是控件。 */
tr.clickable:hover .chevron, tr.open .chevron { color: var(--series-1); }
.clickable { cursor: pointer; }
.clickable:focus-visible { outline: 2px solid var(--series-1); outline-offset: -2px; }
.open > td { border-bottom-color: transparent; }
.detail > td { background: var(--hover-wash); }
.right { text-align: right; }
.sr {
  position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap;
}
</style>
