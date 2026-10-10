<script setup>
/**
 * 登录 — the first screen, and the one page the v2 refactor never listed.
 *
 * Phases F, G and H covered list pages, detail pages and the shell. Nobody put
 * the door on the list, so it kept describing a platform that no longer exists:
 * 「离线事件台账」 in the opening sentence (the framing this project retracted
 * after checking it against the brief), three capabilities from before half the
 * system was built, and a footer promising 「仅做只读展示」 that stopped being
 * true when review, dispatch and disposition landed.
 *
 * The cover now names the brief's six tasks, because that is the shape of the
 * thing behind the door and the shape the sidebar is organised by. The one
 * number on it is read live from `/api/health` — the only endpoint open to a
 * signed-out visitor — rather than typed in, so this page cannot go stale the
 * way its predecessor did.
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { COMPANY_LABELS, api, setCompanyLabels } from '../api.js'
import { session, signIn } from '../session.js'

const router = useRouter()
const username = ref('viewer')
const password = ref('')
const error = ref(null)
const busy = ref(false)

async function submit() {
  if (busy.value) return
  busy.value = true
  error.value = null
  try {
    await signIn(username.value.trim(), password.value)
    router.replace(router.currentRoute.value.query.redirect || '/')
  } catch (exception) {
    error.value = exception.message
    password.value = ''
  } finally {
    busy.value = false
  }
}

// The offering calendar the platform actually uses to scale risk. It is the
// project's own subject matter, which is why it stands in for decoration here.
const PHASES = [
  { label: '申报受理', at: '2025-12-30', coefficient: '1.10' },
  { label: '上会审议', at: '2026-05-20', coefficient: '1.25' },
  { label: '提交注册', at: '2026-05-27', coefficient: '1.20' },
  { label: '发行询价', at: '2026-07-09', coefficient: '1.30' },
  { label: '上市后', at: '2026-07-24', coefficient: '1.00' },
]
/* 系数画成竖条：1.00 → 8px，1.30 → 28px。系数只有五个、区间固定，这是一张常量表的
   换算，不是聚合——页面「不做算术」管的是库里的数。最高的那一处（发行询价）实色。 */
const PEAK = Math.max(...PHASES.map((phase) => Number(phase.coefficient)))
const barHeight = (coefficient) => `${Math.round(8 + ((Number(coefficient) - 1) / 0.3) * 20) / 16}rem`

/** 六个能力，只留名字。
 *
 *  这块东西减了两轮：先是每条下面的一句注解，再是「一二三四五六」的序号。
 *  两次减掉的都是同一种东西——门外替门里提前解释。剩下六个词并排，读者一眼
 *  扫过去就知道里面有什么，这就够了。 */
const TASKS = [
  '多源事件识别', '要素抽取与标准化', '可信度评估与辨伪',
  '同源聚合与去重', '事件脉络追踪', '影响分级与预警推送',
]

/** 每个能力一个线性图标，替掉此前那道统一的蓝色短杠——六张卡终于能一眼分开。
 *  图标是装饰：名字已经写在卡上，所以全部 aria-hidden；两份卡片画的是同一组，
 *  接缝处才看不出换了一份。圆写成两段弧的 path，一个图标一组 d 就够。 */
const dot = (x, y, r = 1.8) => `M${x - r} ${y}a${r} ${r} 0 1 0 ${2 * r} 0a${r} ${r} 0 1 0 ${-2 * r} 0`
const ICONS = {
  多源事件识别: ['M3 5h5l6 7', 'M3 12h11', 'M3 19h5l6-7', dot(18, 12, 2.2)],
  要素抽取与标准化: ['M7 4H4v16h3', 'M17 4h3v16h-3', 'M8.5 9h7', 'M8.5 12h4.5', 'M8.5 15h6'],
  可信度评估与辨伪: ['M12 3l7 3v5c0 4.6-3 7.9-7 10-4-2.1-7-5.4-7-10V6z', 'M9 12l2.2 2.2L15.5 10'],
  同源聚合与去重: [dot(5, 6), dot(5, 18), dot(19, 12, 2.2), 'M7 7l9.5 4', 'M7 17l9.5-4'],
  事件脉络追踪: ['M3 12h18', dot(6, 12), dot(12, 12), dot(18, 12), 'M12 5v4', 'M18 15v4'],
  影响分级与预警推送: ['M5 19v-3', 'M10 19v-6', 'M15 19v-9', 'M20 19v-13'],
}

/** The only figure on this page, and it is fetched rather than written down.
 *  A number typed into a login screen is a number nobody updates.
 *
 *  公司名同理：此前这一行写死了两家公司，加第三家时登录页永远只说两家。现在与事件数取自
 *  同一个响应；取不到时这一行整个消失，而不是印一个没人会去更新的名单。 */
const events = ref(null)
const companies = computed(() => Object.values(COMPANY_LABELS).join(' · '))
onMounted(async () => {
  try {
    const body = await api.health()
    events.value = body.events
    setCompanyLabels(body.companies)
  } catch {
    events.value = null
  }
})
</script>

<template>
  <div class="login">
    <!-- Left: what this platform is. Right: the door. Keeping them apart lets
         the form stay a form instead of carrying the whole first impression. -->
    <section class="cover">
      <div class="cover-inner">
        <div class="mark">
          <span class="mark-dot" aria-hidden="true"></span>
          上市公司事件智能平台
        </div>

        <h1 class="cover-title">
          从公开披露与媒体报道中，<br />还原<em>可核验</em>的公司事件。
        </h1>
        <p class="cover-lede">
          {{ companies }}<template v-if="events">
            <span v-if="companies" class="dot" aria-hidden="true">/</span>
            <b class="mono">{{ events.toLocaleString() }}</b> 起事件</template>
        </p>

      </div>

      <!-- 通栏出血的飘带：脱开 .cover-inner 的 700px 限宽，横跨整个封面。
           两份卡片，轨道平移一半宽度后回到原点——接缝正好是下一份的第一张，
           所以循环无缝。第二份对读屏隐藏，否则六项能力会被念两遍。 -->
      <div class="ribbon">
        <div class="ribbon-track">
          <ul class="ribbon-set">
            <li v-for="item in TASKS" :key="item" class="ribbon-card">
              <svg class="ribbon-icon" viewBox="0 0 24 24" aria-hidden="true">
                <path v-for="d in ICONS[item]" :key="d" :d="d" />
              </svg>{{ item }}
            </li>
          </ul>
          <ul class="ribbon-set" aria-hidden="true">
            <li v-for="item in TASKS" :key="`echo-${item}`" class="ribbon-card">
              <svg class="ribbon-icon" viewBox="0 0 24 24" aria-hidden="true">
                <path v-for="d in ICONS[item]" :key="d" :d="d" />
              </svg>{{ item }}
            </li>
          </ul>
        </div>
      </div>

      <div class="cover-tail">
        <div class="calendar" role="img"
             aria-label="披露阶段日历：申报受理 1.10、上会审议 1.25、提交注册 1.20、发行询价 1.30、上市后 1.00">
          <div class="calendar-head">披露阶段风险系数</div>
          <div class="axis" aria-hidden="true"></div>
          <div class="phases">
            <div v-for="(phase, index) in PHASES" :key="phase.label" class="phase"
                 :class="{ peak: Number(phase.coefficient) === PEAK }">
              <span class="phase-bar" aria-hidden="true"
                    :style="{ height: barHeight(phase.coefficient), '--bar-delay': `${0.3 + index * 0.12}s` }"></span>
              <span class="node" aria-hidden="true"></span>
              <span class="phase-label">{{ phase.label }}</span>
              <span class="phase-at mono">{{ phase.at }}</span>
              <span class="phase-coefficient mono">×{{ phase.coefficient }}</span>
            </div>
          </div>
        </div>

        <!-- 「仅做只读展示」 was true once and stopped being true when review,
             dispatch and disposition landed. The sentence that survives is the
             one still enforced: the analysis layer is opened `mode=ro`, so the
             three write paths each own their own tables and cannot touch it. -->
        <div class="cover-foot">数据来源均为公开渠道 · 人工记录不改写处理层结论</div>
      </div>
    </section>

    <section class="gate">
      <form class="form" @submit.prevent="submit">
        <h2 class="form-title">登录</h2>

        <label class="field">
          <span class="field-label">账号</span>
          <input v-model="username" type="text" autocomplete="username" required
                 spellcheck="false" autocapitalize="off" />
        </label>

        <label class="field">
          <span class="field-label">密码</span>
          <input v-model="password" type="password" autocomplete="current-password" required
                 placeholder="请输入密码" />
        </label>

        <!-- Errors are announced, not just coloured: a login failure read by a
             screen reader must arrive as text. -->
        <p v-if="error" class="error" role="alert">{{ error }}</p>

        <button class="submit" type="submit" :disabled="busy || !password">
          {{ busy ? '验证中…' : '进入平台' }}
        </button>

      </form>
    </section>
  </div>
</template>

<style scoped>
/* 左半是一张印出来的封面，右半是一扇门。

   封面此前在三套主题下都保持墨色，理由写的是「它不是跟随阅读表面的面板」。
   2026-09-22 撤销：用户切主题时说「登录页也是这个问题」——左半不跟着变，
   整个换肤动作看起来就是坏的。一个说得通的理由，抵不过一个看起来没生效的控件。

   封面因此不再自带一套颜色，它取的是各主题自己那张纸：底是 --surface-0（最沉
   的一级），两束光是 --surface-1 与 --surface-2，字是 --text-* 三级，重音是
   --series-1。深色主题下这套值本身就是深海军蓝，与改之前几乎一样；浅色与护眼
   下它成了一张浅封面。 */
.login {
  min-height: 100vh;
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(400px, 0.8fr);
  background: var(--page);
}

.cover {
  position: relative;
  isolation: isolate;
  /* 左上一束冷光、右下一束更深的蓝，垫在近黑的底上。单色平铺会板。 */
  background:
    radial-gradient(115% 78% at 6% -4%, var(--surface-1) 0%, transparent 58%),
    radial-gradient(88% 66% at 98% 104%, var(--surface-2) 0%, transparent 52%),
    var(--surface-0);
  color: var(--text-primary);
  /* 飘带要突破这层内边距通栏出血。三个断点上的取值写成变量，负边距才不会
     在改版式时和它漂开。 */
  --cover-pad: 76px;
  padding: 76px var(--cover-pad) 36px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  overflow: hidden;
}
.cover::before {
  /* 极淡的账簿方格——这个平台是从文件里长出来的。 */
  content: '';
  position: absolute;
  inset: 0;
  z-index: -1;
  background-image: linear-gradient(var(--hover-wash) 1px, transparent 1px),
    linear-gradient(90deg, var(--hover-wash) 1px, transparent 1px);
  background-size: 64px 64px;
  mask-image: radial-gradient(125% 95% at 10% 4%, #000 22%, transparent 76%);
  pointer-events: none;
}
.cover-inner,
.cover-tail { position: relative; max-width: 700px; }

.mark {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12.5px;
  letter-spacing: 0.16em;
  color: var(--text-secondary);
}
.mark-dot {
  position: relative;
  width: 6px; height: 6px; border-radius: 50%; background: var(--series-1); flex: none;
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--series-1) 16%, transparent);
}
/* 品牌点外一圈极慢的外扩光环：3s 一轮，只是「这里是活的」，不是在招手。 */
.mark-dot::after {
  content: '';
  position: absolute;
  inset: -4px;
  border-radius: 50%;
  border: 1px solid color-mix(in srgb, var(--series-1) 45%, transparent);
  animation: mark-ring 3s ease-out infinite;
}
@keyframes mark-ring {
  from { transform: scale(0.6); opacity: 0.9; }
  to { transform: scale(2.2); opacity: 0; }
}

.cover-title {
  font-size: clamp(34px, 3.9vw, 58px);
  line-height: 1.18;
  letter-spacing: -0.038em;
  font-weight: 600;
  margin: 64px 0 0;
  text-wrap: balance;
}
.cover-title em {
  font-style: normal;
  color: var(--series-1);
  /* 一条细下划线代替换色块：强调三个字，但不打断行的重心。画成背景而不是 box-shadow，
     是为了能让它在进页时从左往右画出来——box-shadow 没有「长度」可以动。 */
  background: linear-gradient(color-mix(in srgb, var(--series-1) 26%, transparent),
    color-mix(in srgb, var(--series-1) 26%, transparent)) no-repeat left bottom / 100% 0.14em;
  animation: underline-draw 0.7s cubic-bezier(0.2, 0.7, 0.2, 1) 0.35s both;
}
@keyframes underline-draw { from { background-size: 0% 0.14em; } to { background-size: 100% 0.14em; } }

.cover-lede {
  margin: 26px 0 0;
  font-size: 13.5px;
  letter-spacing: 0.05em;
  color: var(--text-secondary);
}
.cover-lede .dot { margin: 0 8px; color: var(--text-muted); }
.cover-lede b { color: var(--text-primary); font-weight: 560; }

/* 六个能力做成向左飘的卡片带。这块减过三轮：先是每条下面的注解，再是
   「一二三四五六」的序号，最后连竖线分隔的静态排列也换掉了。

   循环靠渲染两份、轨道整体平移一半宽度实现，接缝处正好接上下一份的第一张。
   两份必须等宽——`translateX(-50%)` 才恰好等于一份——所以卡间距用 gap，
   每份两侧再各留半个 gap 的 padding，接缝间距就和内部完全一致。 */
.ribbon {
  position: relative;
  margin: 52px 0 0;
  overflow: hidden;
  margin-inline: calc(var(--cover-pad) * -1);
  /* 两端渐隐：卡片像是从封面边缘之外飘进来，而不是被硬切一刀。 */
  mask-image: linear-gradient(90deg, transparent 0, #000 6%, #000 94%, transparent 100%);
  -webkit-mask-image: linear-gradient(90deg, transparent 0, #000 6%, #000 94%, transparent 100%);
}
.ribbon-track {
  display: flex;
  width: max-content;
  will-change: transform;
  animation: ribbon-drift 26s linear infinite;
}
/* 26s 一圈，约 50px/s。40s 时几乎看不出在动，像是页面卡住了；再快就开始
   追着眼睛跑，第一屏不该有那种东西。 */
@keyframes ribbon-drift {
  from { transform: translateX(0); }
  to { transform: translateX(-50%); }
}
/* 鼠标与键盘拿到同一个暂停。持续运动必须可停，这是唯一的停法。 */
.ribbon:hover .ribbon-track,
.ribbon:focus-within .ribbon-track { animation-play-state: paused; }

.ribbon-set {
  display: flex;
  list-style: none;
  margin: 0;
  gap: 14px;
  padding: 0 7px;
}
/* 第一版是纯描边的空盒子，在墨底加暗格的背景上读起来像一排没填的输入框。
   三处改动让它成为「物」而不是「框」：极淡的自上而下填充给出体积，顶边一道
   内高光当作受光面，底下一层极浅的投影把它从背景上托起来。
   左侧那道蓝色短杠呼应页首品牌点，是六张卡唯一的彩色，也是它们唯一的重音。 */
.ribbon-card {
  position: relative;
  /* 流光要被裁在圆角里，否则会溢到卡外变成一条横杠。 */
  overflow: hidden;
  flex: none;
  display: flex;
  align-items: center;
  gap: 11px;
  /* 六个词长短不一（六字到九字）。给一个下限，飘带才有节奏，否则宽窄参差
     像是排版没做完。 */
  min-width: 176px;
  padding: 16px 22px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: linear-gradient(180deg, var(--hover-wash), transparent);
  box-shadow: inset 0 1px 0 var(--hover-wash), 0 1px 3px color-mix(in srgb, var(--text-primary) 14%, transparent);
  font-size: 13.5px;
  line-height: 1.2;
  letter-spacing: 0.01em;
  color: var(--text-primary);
  white-space: nowrap;
  transition: border-color 0.14s ease, background 0.14s ease, color 0.14s ease;
}
.ribbon-icon {
  flex: none;
  width: 17px;
  height: 17px;
  fill: none;
  stroke: var(--series-1);
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: transform 0.18s ease;
}
.ribbon-card:hover .ribbon-icon { transform: scale(1.08); }
.ribbon-card:hover {
  border-color: var(--baseline);
  background: linear-gradient(180deg, var(--surface-2), var(--hover-wash));
  color: var(--text-primary);
}

/* 流光：一道倾斜的高光扫过卡面。
   放在卡里而不是整条带子上——铺在带子上会连卡片之间的缝隙一起提亮，看着像
   一块擦不掉的污渍；只在卡内扫，缝隙保持暗的，光才像是被卡片接住的。

   延迟按卡序错开，于是光是「沿着带子走过去」而不是六张一起闪。两份卡片的
   第 n 张用同一个延迟，相位因此完全一致——否则接缝处会出现一张相位不同的
   卡，那正是这种飘带最容易露馅的地方。 */
.ribbon-card::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: -55%;
  width: 42%;
  pointer-events: none;
  transform: skewX(-18deg);
  background: linear-gradient(90deg,
    transparent,
    var(--hover-wash) 30%,
    color-mix(in srgb, var(--series-1) 16%, transparent) 50%,
    var(--hover-wash) 70%,
    transparent);
  animation: ribbon-sheen 3.2s ease-in-out infinite;
}
/* 3.2s 一轮：扫过约 1.2s，停约 2s。5.6s 那版隔太久，两次之间人已经忘了它会
   亮，光就不像光而像偶发的闪烁。留着这段停顿是为了不变成霓虹灯——真要再密，
   往下调周期即可，别去掉停顿。 */
@keyframes ribbon-sheen {
  0%, 8% { transform: translateX(0) skewX(-18deg); }
  46%, 100% { transform: translateX(400%) skewX(-18deg); }
}
.ribbon-card:nth-child(1)::after { animation-delay: 0s; }
.ribbon-card:nth-child(2)::after { animation-delay: 0.16s; }
.ribbon-card:nth-child(3)::after { animation-delay: 0.32s; }
.ribbon-card:nth-child(4)::after { animation-delay: 0.48s; }
.ribbon-card:nth-child(5)::after { animation-delay: 0.64s; }
.ribbon-card:nth-child(6)::after { animation-delay: 0.8s; }

/* 停住飘动时也停住流光，否则悬停时卡片还在自顾自地闪。 */
.ribbon:hover .ribbon-card::after,
.ribbon:focus-within .ribbon-card::after { animation-play-state: paused; }

/* 飘带已经把封面上下两块隔开了，日历不必再自带上边距——`space-between`
   分配的空隙加上 66px 会把尾部压到底边。 */
.calendar { margin-top: 0; }
.calendar-head {
  font-size: 10.5px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-muted);
  /* 给系数竖条留出高度：最高那根 28px，立在轴线上方。 */
  margin-bottom: 38px;
}
.axis { height: 1px; background: linear-gradient(90deg, var(--baseline), var(--gridline)); }
.phases { display: flex; gap: 8px; margin-top: -5px; }
.phase { flex: 1; display: flex; flex-direction: column; gap: 3px; min-width: 0; position: relative; }
/* 系数竖条立在轴线上方、节点正上：高度即系数，入场时依次长出来。 */
.phase-bar {
  position: absolute;
  left: 4px;
  bottom: calc(100% - 4px);
  width: 2px;
  border-radius: 2px 2px 0 0;
  background: color-mix(in srgb, var(--series-1) 38%, transparent);
  transform-origin: bottom;
  animation: bar-grow 0.6s cubic-bezier(0.2, 0.7, 0.2, 1) var(--bar-delay, 0s) both;
}
.peak .phase-bar { background: var(--series-1); }
@keyframes bar-grow { from { transform: scaleY(0); } to { transform: scaleY(1); } }
/* 系数最高的那一处（发行询价 ×1.30）节点缓慢呼吸——这张日历要讲的就是这一个点。 */
.peak .node { background: var(--series-1); animation: node-breathe 2.4s ease-in-out infinite; }
@keyframes node-breathe {
  0%, 100% { box-shadow: 0 0 0 3px color-mix(in srgb, var(--series-1) 10%, transparent); }
  50% { box-shadow: 0 0 0 6px color-mix(in srgb, var(--series-1) 22%, transparent); }
}
.peak .phase-coefficient { font-weight: 650; }
.node {
  width: 9px; height: 9px; border-radius: 50%;
  border: 1px solid var(--series-1); background: var(--surface-0); margin-bottom: 9px;
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--series-1) 10%, transparent);
}
.phase-label { font-size: 12px; color: var(--text-primary); }
.phase-at { font-size: 10.5px; color: var(--text-muted); }
.phase-coefficient { font-size: 11.5px; color: var(--series-1); }

.cover-foot {
  position: relative;
  margin-top: 48px;
  padding-top: 18px;
  border-top: 1px solid var(--border);
  font-size: 11px;
  letter-spacing: 0.02em;
  color: var(--text-muted);
}

/* 门这一侧：留白更宽，字阶更分明，控件更高——手感上的「稳」多半来自尺寸
   而不是装饰。这里只剩两个输入框、一个按钮和一行说明。 */
.gate { display: flex; align-items: center; justify-content: center; padding: 64px 56px; }
/* 不给表单加卡。试过一版有描边、圆角、软阴影的，观感反而更像一个登录弹窗
   被贴在页面中间。撑住它的是字阶、留白和控件尺寸。 */
.form { width: 100%; max-width: 380px; }
.form-title {
  font-size: 28px;
  font-weight: 620;
  letter-spacing: 0.12em;
  text-indent: 0.12em;   /* 字距会在末字右侧多留一格，缩进抵掉，视觉才真居中 */
  text-align: center;
  margin: 0 0 40px;
}

.field { display: block; margin-bottom: 20px; }
/* 原来是 11px + 0.1em 字距 + uppercase——那是拉丁排版的小标签做法。中文没有
   大小写，`text-transform` 什么也没做，剩下的就是又小又散的两个字。 */
.field-label {
  display: block;
  font-size: 12.5px;
  color: var(--text-secondary);
  margin-bottom: 8px;
}
.field input {
  width: 100%;
  font: inherit;
  font-size: 14px;
  color: var(--text-primary);
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 9px;
  padding: 13px 15px;
  min-height: 48px;
  transition: border-color 0.14s ease, box-shadow 0.14s ease;
}
.field input::placeholder { color: var(--text-muted); }
.field input:hover { border-color: var(--text-muted); }
.field input:focus-visible {
  outline: none;
  border-color: var(--series-1);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--series-1) 16%, transparent);
}

.submit {
  width: 100%;
  min-height: 50px;
  margin-top: 18px;
  border: 1px solid transparent;
  border-radius: 9px;
  background: var(--series-1);
  color: var(--surface-1);
  font-size: 14.5px;
  font-weight: 560;
  letter-spacing: 0.08em;
  text-indent: 0.08em;
  transition: filter 0.14s ease, box-shadow 0.14s ease;
}
.submit:hover:not(:disabled) {
  background: var(--series-1);
  color: var(--surface-1);
  filter: brightness(1.07);
  box-shadow: 0 2px 10px color-mix(in srgb, var(--series-1) 28%, transparent);
}
.submit:disabled { opacity: 0.38; }

.error {
  font-size: var(--fs-body);
  color: var(--status-critical);
  border-left: 2px solid var(--status-critical);
  padding: 6px 0 6px 10px;
  margin: 0 0 14px;
}

.hint { font-size: 11.5px; line-height: 1.75; color: var(--text-muted); margin: 20px 0 0; }
.hint code { font-size: 11px; }

/* 全站唯一的动画，也是全站第一次需要这条守卫。关掉之后飘带变成一排静态的
   描边卡：不出血、不复制、可换行，六个词一个不少。没有任何东西只存在于运动
   之中，这是这条飘带能加上来的前提。 */
@media (prefers-reduced-motion: reduce) {
  .ribbon {
    overflow: visible;
    margin-inline: 0;
    mask-image: none;
    -webkit-mask-image: none;
  }
  .ribbon-track { animation: none; width: auto; }
  .ribbon-card::after { animation: none; display: none; }
  .mark-dot::after { animation: none; display: none; }
  .cover-title em { animation: none; }
  .phase-bar { animation: none; }
  .peak .node { animation: none; }
  .ribbon-icon { transition: none; }
  .ribbon-set { flex-wrap: wrap; padding: 0; }
  .ribbon-set[aria-hidden='true'] { display: none; }
}

/* 窄下来先让掉日历——六个词是这一屏的信息，日历是它的装饰。 */
@media (max-width: 1200px) {
  .cover { --cover-pad: 44px; padding: 52px var(--cover-pad) 28px; }
  .ribbon { margin-top: 40px; }
  .calendar { display: none; }
}

@media (max-width: 900px) {
  .login { grid-template-columns: 1fr; }
  .cover { --cover-pad: 28px; padding: 40px var(--cover-pad) 26px; }
  .cover-title { margin-top: 30px; }
  .ribbon { display: none; }
  .cover-foot { margin-top: 30px; }
  .gate { padding: 40px 28px 64px; }
}
</style>
