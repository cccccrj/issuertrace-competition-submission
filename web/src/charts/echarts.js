/** The single place ECharts is imported from.
 *
 *  Importing the whole library pulls in maps, geo projections and a dozen chart
 *  types this platform will never draw.  Registering only what is used keeps the
 *  bundle honest and, more usefully, makes the set of forms the interface is
 *  allowed to draw a thing you can read in one screen: if a chart type is not
 *  here, no page can quietly start using it.
 *
 *  The renderer is **SVG, not canvas**, for three reasons that all matter here:
 *  the report page prints, and a canvas prints as a bitmap; jsdom has no canvas,
 *  so tests would need a native dependency; and text in SVG stays selectable and
 *  reaches a screen reader's DOM even before the table fallback.
 */
import * as echarts from 'echarts/core'
import {
  BarChart,
  CustomChart,
  HeatmapChart,
  LineChart,
  ScatterChart,
} from 'echarts/charts'
import {
  AriaComponent,
  DatasetComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  TooltipComponent,
  VisualMapComponent,
} from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'

echarts.use([
  // Forms. A pie chart is deliberately absent: every part-to-whole in this
  // platform is a stacked bar, which compares across categories where a pie
  // cannot.  Sankey is absent too -- the one flow in the data (issue status
  // transitions) is a 5x5 matrix and reuses the heatmap.
  BarChart,
  LineChart,
  ScatterChart,
  HeatmapChart,
  CustomChart,   // ranged bars: the disclosure-phase band under the trend
  // Chrome.
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DatasetComponent,
  MarkLineComponent,   // quadrant reference lines, the shipped threshold marker
  MarkAreaComponent,
  // Without this, `visualMap` is not ignored loudly — it is ignored silently,
  // and both heatmaps fell back to one flat ECharts default blue. A cell of 0
  // and a cell of 769 were the same colour for as long as the component was
  // missing, which is worse than having no colour channel at all: it says the
  // matrix is uniform.
  VisualMapComponent,
  AriaComponent,       // drives the decal (texture) channel for the CVD case
  SVGRenderer,
])

export default echarts
