#!/usr/bin/env python3
"""Generate the three white-paper figures as standalone SVG (1920×1080, dark theme).

Hand-placed on a grid rather than drawn in a tool, so a number that changes is
one edit here and a re-run, not a session in a drawing program:

    python3 docs/figures/build_figures.py          # writes fig1/fig2/fig3 *.svg next to this file
    cd web && node scripts/render-figures.mjs      # renders each to a 3840×2160 PNG

Every label is a <text> element (no outlined glyphs), so PowerPoint's
"Convert to Shape" leaves the words editable.  Figures are self-contained: no
external fonts, images or scripts.
"""
from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent
W, H = 1920, 1080
FONT = "'Microsoft YaHei','PingFang SC','Noto Sans CJK SC','WenQuanYi Zen Hei',sans-serif"

BG = "#0B1220"
PANEL = "#0F1729"
BAND = "#111A2E"
BAND_EDGE = "#1E2A44"
CYAN = "#38BDF8"
CYAN_SOFT = "#7DD3FC"
AMBER = "#F59E0B"
AMBER_SOFT = "#FBBF24"
TEXT = "#E2E8F0"
MUTED = "#94A3B8"
DIM = "#64748B"
GREEN = "#34D399"


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------

def text(x, y, s, size=16, color=TEXT, weight="normal", anchor="start", opacity=None, spacing=None):
    extra = f' opacity="{opacity}"' if opacity is not None else ""
    extra += f' letter-spacing="{spacing}"' if spacing is not None else ""
    return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}" '
            f'text-anchor="{anchor}"{extra}>{escape(s)}</text>')


def rect(x, y, w, h, fill=PANEL, stroke=BAND_EDGE, rx=12, sw=1.5, extra=""):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{sw}" {extra}/>')


def arrow(points, color=CYAN, width=2.2, dashed=False, marker="arrow"):
    d = "M " + " L ".join(f"{x} {y}" for x, y in points)
    dash = ' stroke-dasharray="9 7"' if dashed else ""
    return (f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}"{dash} '
            f'stroke-linejoin="round" marker-end="url(#{marker})"/>')


def pill(cx, cy, label, color=CYAN, size=14, pad=14, fill=BG):
    width = int(len(label) * size * 0.95 + pad * 2)
    return (rect(cx - width / 2, cy - size, width, size * 2, fill=fill, stroke=color, rx=size, sw=1.2)
            + text(cx, cy + size * 0.36, label, size=size, color=color, anchor="middle"))


def defs():
    return f'''<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#0B1220"/><stop offset="0.55" stop-color="#0C1526"/><stop offset="1" stop-color="#0A1020"/>
  </linearGradient>
  <radialGradient id="glow" cx="0.5" cy="0.45" r="0.6">
    <stop offset="0" stop-color="{CYAN}" stop-opacity="0.10"/><stop offset="1" stop-color="{CYAN}" stop-opacity="0"/>
  </radialGradient>
  <linearGradient id="card" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#15274A"/><stop offset="1" stop-color="#0F1B33"/>
  </linearGradient>
  <linearGradient id="cardStar" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#3A2A0E"/><stop offset="1" stop-color="#1A1610"/>
  </linearGradient>
  <linearGradient id="bandHead" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#16274A"/><stop offset="1" stop-color="#0F1729"/>
  </linearGradient>
  <pattern id="dots" width="32" height="32" patternUnits="userSpaceOnUse">
    <circle cx="2" cy="2" r="1.2" fill="#1C2A45"/>
  </pattern>
  <filter id="soft" x="-20%" y="-20%" width="140%" height="140%">
    <feGaussianBlur stdDeviation="6" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
  </filter>
  <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M0 0 L10 5 L0 10 z" fill="{CYAN}"/>
  </marker>
  <marker id="arrowAmber" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M0 0 L10 5 L0 10 z" fill="{AMBER}"/>
  </marker>
  <marker id="arrowGreen" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M0 0 L10 5 L0 10 z" fill="{GREEN}"/>
  </marker>
</defs>'''


def frame(title, subtitle, body, label):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="{escape(label)}">
{defs()}
<g font-family="{FONT}">
<rect width="{W}" height="{H}" fill="url(#bg)"/>
<rect width="{W}" height="{H}" fill="url(#dots)"/>
<rect width="{W}" height="{H}" fill="url(#glow)"/>
<rect x="40" y="44" width="6" height="48" rx="3" fill="{CYAN}"/>
{text(62, 78, title, 38, TEXT, "bold")}
{text(64, 112, subtitle, 18, MUTED)}
{body}
</g>
</svg>
'''


def card(x, y, w, h, title, sub="", star=False, num=None, title_size=19, sub_size=14):
    stroke = AMBER if star else CYAN
    fill = "url(#cardStar)" if star else "url(#card)"
    out = [rect(x, y, w, h, fill=fill, stroke=stroke, rx=12, sw=1.6,
                extra='stroke-opacity="0.85"' + (' filter="url(#soft)"' if star else ""))]
    cx = x + w / 2
    lines = title.split("\n")
    sub_lines = sub.split("\n") if sub else []
    block = len(lines) * (title_size + 6) + len(sub_lines) * (sub_size + 6) + (20 if num else 0)
    ty = y + (h - block) / 2 + title_size
    if num:
        out.append(text(cx, ty - 2, num, 13, AMBER_SOFT if star else CYAN_SOFT, "bold", "middle", spacing=1))
        ty += 20
    for line in lines:
        out.append(text(cx, ty, line, title_size, AMBER_SOFT if star else TEXT, "bold", "middle"))
        ty += title_size + 6
    ty += 2
    for line in sub_lines:
        out.append(text(cx, ty, line, sub_size, MUTED, "normal", "middle"))
        ty += sub_size + 6
    return "\n".join(out)


def row(x0, x1, y, h, items, gap=16, **kw):
    n = len(items)
    w = (x1 - x0 - gap * (n - 1)) / n
    return "\n".join(card(round(x0 + i * (w + gap), 1), y, round(w, 1), h, *item[:2],
                          **{**kw, **(item[2] if len(item) > 2 else {})})
                     for i, item in enumerate(items))


# ---------------------------------------------------------------------------
# Figure 1 — layered architecture
# ---------------------------------------------------------------------------

def figure1() -> str:
    body = []
    x0, x1 = 178, 1742           # main band area
    label_w = 188
    bx0, bx1 = x0 + label_w + 16, x1 - 16
    bands = [  # (name, english, stat, height, items, kwargs)
        ("应用层", "APPLICATION", "31 页面 · 95 API", 138, [
            ("风险预警中心", "今日待研判排序"), ("研判档案", "七要素 · 证据链"),
            ("研判助手 Agent", "ReAct · 逐句出处"), ("复核与处置", "双盲复核 · 状态机"),
            ("多通道推送", "站内 · 邮件 · Webhook"), ("研判报告", "Word 一键导出")], {}),
        ("数据层", "DATA", "只读挂载 · 人机分库", 138, [
            ("SQLite 分析库", "处理层结论 · mode=ro"), ("FTS5 证据索引", "93,209 段 · BM25"),
            ("BGE-M3 向量近邻", "3,922 主张 × Top-20"), ("人工决策库", "复核 · 处置 · 审计")], {}),
        ("智能分析层", "INTELLIGENCE", "10 大研判引擎", 214, [
            ("事件抽取", "大模型\n+ Schema", {"num": "01"}),
            ("要素标准化", "七要素\n主体归一", {"num": "02"}),
            ("披露版本\n比对 ★", "招股书\n四稿链式", {"num": "03", "star": True}),
            ("双路召回", "BM25\n+ BGE-M3", {"num": "04"}),
            ("主张核验", "四值判定\n7 类偏差", {"num": "05"}),
            ("可信评估", "判定层\n+ 排序分", {"num": "06"}),
            ("聚合去重", "SimHash\n+ LSH", {"num": "07"}),
            ("议题台账 ★", "生命周期\n五态", {"num": "08", "star": True}),
            ("传播图谱 ★", "9 类边\n因果链", {"num": "09", "star": True}),
            ("阶段风险", "IPO 阶段\n系数加权", {"num": "10"})], {"gap": 10, "title_size": 18, "sub_size": 13}),
        ("采集与存证层", "INGESTION", "4,339 版本存证", 138, [
            ("合规调度", "robots · 限速 · 熔断"), ("内容寻址存证", "SHA-256 · 版本链"),
            ("按页解析", "PyMuPDF · 页码锚定"), ("中英繁简切词", "二元组 · 词 · 繁简折叠"),
            ("实体消歧", "别名 · 时效别名")], {}),
        ("数据源层", "SOURCES", "55 信源 · S0–S3 分级", 138, [
            ("交易所 · 巨潮", "法定披露 · S0"), ("监管与政府", "证监会 · 工信部 · 美国政府"),
            ("公司官方", "官网 · IR · 互动易"), ("国内外媒体", "财经 · 行业 · 海外"),
            ("行情与财务", "日线行情 · 财务指标")], {}),
    ]
    flows = ["只读查询（mode=ro）", "结论写入 · 每条可追溯到原文页码", "带页码文本 · 证据段落", "合规抓取 · 条件请求 · 原始字节"]
    gap = 30
    y = 140
    tops = []
    for index, (name, english, stat, h, items, kw) in enumerate(bands):
        tops.append((y, h))
        body.append(rect(x0, y, x1 - x0, h, fill=BAND, stroke=BAND_EDGE, rx=16, sw=1.2))
        body.append(rect(x0, y, label_w, h, fill="url(#bandHead)", stroke="none", rx=16, sw=0))
        mid = y + h / 2
        body.append(text(x0 + 24, mid - 10, name, 24, TEXT, "bold"))
        body.append(text(x0 + 24, mid + 14, english, 12, DIM, "bold", spacing=2))
        body.append(text(x0 + 24, mid + 38, stat, 14, CYAN_SOFT))
        body.append(row(bx0, bx1, y + 16, h - 32, items, **kw))
        y += h + gap
    # flows between bands: data moves upward
    for index, label in enumerate(flows):
        upper_y, upper_h = tops[index]
        lower_y, _ = tops[index + 1]
        top, bottom = upper_y + upper_h, lower_y
        for ax in (bx0 + 120, bx1 - 120):
            body.append(arrow([(ax, bottom - 2), (ax, top + 4)], width=2))
        body.append(pill((bx0 + bx1) / 2, (top + bottom) / 2, "▲ " + label, size=13))
    # side pillars
    def pillar(x, title, chips, color):
        out = [rect(x, 140, 128, y - gap - 140, fill=PANEL, stroke=color, rx=16, sw=1.2,
                    extra='stroke-opacity="0.6"')]
        ty = 196
        for ch in title:
            out.append(text(x + 64, ty, ch, 24, color, "bold", "middle"))
            ty += 32
        cy = ty + 26
        for chip in chips:
            out.append(rect(x + 10, cy, 108, 34, fill=BG, stroke=BAND_EDGE, rx=8, sw=1))
            out.append(text(x + 64, cy + 22, chip, 13, TEXT, anchor="middle"))
            cy += 44
        return "\n".join(out)
    body.append(pillar(36, "纵深安全合规", ["四级 RBAC", "PBKDF2 24万轮", "分析库只读", "只追加审计", "SSRF 防护", "注入隔离", "默认演练推送"], CYAN))
    body.append(pillar(1756, "可复现评测体系", ["E1–E7 任务", "train/test 隔离", "run_id 留痕", "预注册门槛", "基线对照", "后端 1,797 测试", "前端 403 测试"], GREEN))
    # legend
    body.append(rect(1452, 50, 26, 18, fill="url(#cardStar)", stroke=AMBER, rx=5, sw=1.4))
    body.append(text(1488, 65, "★ 三大差异化引擎", 16, AMBER_SOFT))
    body.append(text(1488, 95, "▲ 数据自下而上流动", 16, CYAN_SOFT))
    body.append(text(W / 2, 1062, "数字口径 2026-10-04 · 4,190 文档 / 4,339 版本 · 1,907 事件 · 3,922 主张逐条核验 · 41 议题 · 281 预警",
                     14, DIM, anchor="middle"))
    return frame("上市公司事件智识平台 · 总体技术架构", "五层分层架构 · 证据从原始字节贯穿到研判结论",
                 "\n".join(body), "平台五层架构：数据源、采集与存证、智能分析、数据、应用；两侧为安全合规与评测体系")


# ---------------------------------------------------------------------------
# Figure 2 — the adjudication engine, one report from intake to alert
# ---------------------------------------------------------------------------

def node(x, y, w, h, num, title, sub, metric, star=False, accent=None):
    stroke = AMBER if star else (accent or CYAN)
    out = [rect(x, y, w, h, fill="url(#cardStar)" if star else "url(#card)", stroke=stroke, rx=14, sw=1.7,
                extra='stroke-opacity="0.9"' + (' filter="url(#soft)"' if star else ""))]
    if num:
        out.append(f'<circle cx="{x + 28}" cy="{y + 28}" r="15" fill="{BG}" stroke="{stroke}" stroke-width="1.5"/>')
        out.append(text(x + 28, y + 33, num, 14, stroke, "bold", "middle"))
    out.append(text(x + w / 2 + (10 if num else 0), y + 36, title, 21, AMBER_SOFT if star else TEXT, "bold", "middle"))
    for i, line in enumerate(sub.split("\n")):
        out.append(text(x + w / 2, y + 68 + i * 22, line, 15, MUTED, anchor="middle"))
    if metric:
        out.append(f'<line x1="{x + 18}" y1="{y + h - 42}" x2="{x + w - 18}" y2="{y + h - 42}" stroke="{BAND_EDGE}" stroke-width="1"/>')
        out.append(text(x + w / 2, y + h - 16, metric, 15, CYAN_SOFT if not star else AMBER_SOFT, "bold", "middle"))
    return "\n".join(out)


def figure2() -> str:
    body = []
    w, h = 250, 158
    xs = [80, 380, 680, 980, 1280, 1580]
    r1, r2 = 392, 640
    # stores and side engines above row 1
    body.append(rect(930, 170, 350, 132, fill=PANEL, stroke=CYAN, rx=14, sw=1.4, extra='stroke-opacity="0.6"'))
    body.append(text(1105, 198, "证据底座", 15, CYAN_SOFT, "bold", "middle", spacing=2))
    body.append(card(946, 212, 155, 76, "FTS5 · BM25", "93,209 段披露", title_size=17, sub_size=13))
    body.append(card(1109, 212, 155, 76, "BGE-M3 向量", "GPU 离线 · Top-20", title_size=17, sub_size=13))
    body.append(node(1330, 162, 470, 140, None, "★ 披露版本比对引擎",
                     "招股书四稿链式比对 · 问询函题号与轮次解析", "1,212 条差异 · 16 道问询 → 80 组对齐", star=True))
    body.append(arrow([(1023, 302), (1023, r1 - 4)], width=2))
    body.append(pill(1023, 348, "关键词召回 前 5", size=13))
    body.append(arrow([(1186, 302), (1186, r1 - 4)], color=GREEN, width=2, marker="arrowGreen"))
    body.append(pill(1186, 348, "语义兜底 补 2", color=GREEN, size=13))
    body.append(arrow([(1405, 302), (1405, r1 - 4)], color=AMBER, width=2, marker="arrowAmber"))
    body.append(pill(1405, 348, "问询路由 · 已解释", color=AMBER, size=13))
    # row 1
    row1 = [
        (None, "多源报道 / 公告", "55 信源合规采集\n内容寻址存证", "2,803 篇准入"),
        ("1", "结构化抽取", "大模型 + JSON Schema\n主张逐字溯源", "1,907 个事件"),
        ("2", "原子主张", "可核验的最小事实\n七要素标准化", "3,922 条主张"),
        ("3", "双路召回", "BM25 前 5 原样保留\n向量只追加、不替换", "候选含证据 89.4%"),
        ("4", "四值核验", "支持 / 部分 / 冲突 / 无法核验\n数值先走规则", "支持 2,091 · 冲突 195"),
        ("5", "两层可信度", "可审计判定层\n+ 四因子排序分", "0.40/0.30/0.15/0.15"),
    ]
    for x, (num, title, sub, metric) in zip(xs, row1):
        body.append(node(x, r1, w, h, num, title, sub, metric))
    for a, b in zip(xs, xs[1:]):
        body.append(arrow([(a + w + 4, r1 + h / 2), (b - 6, r1 + h / 2)], width=2.2))
    # turn down to row 2
    body.append(arrow([(xs[5] + w / 2, r1 + h + 4), (xs[5] + w / 2, r2 - 6)], width=2.2))
    body.append(pill(xs[5] + w / 2 + 2, (r1 + h + r2) / 2, "按议题汇聚", size=13))
    # row 2, right to left
    row2 = [
        ("6", "聚合去重", "SimHash + LSH 8 段\nJaccard + 包含度", "E5 pair-F1 0.8569", False),
        ("7", "★ 议题台账", "文章 → 事件 → 议题\n萌芽 / 升温 / 活跃 / 降温 / 沉寂", "41 个议题", True),
        ("8", "阶段感知风险", "可解释加分项\n× IPO 阶段系数 1.0–1.3", "发行询价期 ×1.3", False),
        ("9", "智能预警", "6 类可读理由 · 7 天合并\n站内 / 邮件 / Webhook", "281 条预警", False),
        (None, "研判人员", "风险预警中心\n研判档案 · 证据链", "一键导出 Word", False),
    ]
    xs2 = list(reversed(xs))[:5]
    for x, (num, title, sub, metric, star) in zip(xs2, row2):
        body.append(node(x, r2, w, h, num, title, sub, metric, star=star))
    for a, b in zip(xs2, xs2[1:]):
        body.append(arrow([(a - 4, r2 + h / 2), (b + w + 6, r2 + h / 2)], width=2.2))
    # agent beside the analyst
    body.append(node(xs[0], r2, w, h, None, "研判助手 Agent", "ReAct 多步查证 · 6 工具\n句柄防编造 · SSE 流式", "每句带出处", accent=GREEN))
    body.append(arrow([(xs[1] - 4, r2 + h / 2), (xs[0] + w + 6, r2 + h / 2)], color=GREEN, width=2.2, marker="arrowGreen"))
    body.append(text((xs[0] + w + xs[1]) / 2, r2 + h / 2 - 14, "追问", 14, GREEN, anchor="middle"))
    # propagation graph feeding dedup / independence
    pg_y = 864
    body.append(node(1430, pg_y, 400, 120, None, "★ 传播图谱与因果链", "9 类传播边 · 独立信源 = 弱连通分量", "2,927 条边 · 1,401 个独立分量", star=True))
    body.append(arrow([(1705, pg_y - 4), (1705, r2 + h + 6)], color=AMBER, width=2, marker="arrowAmber"))
    body.append(pill(1705, (pg_y + r2 + h) / 2, "转载 / 改写边", color=AMBER, size=13))
    # human loop
    lx = [380, 680, 980]
    loop = [("人工复核", "双人背靠背 · Cohen κ"), ("处置状态机", "待研判 → … → 已归档"), ("金标回流", "标定阈值与权重")]
    for x, (title, sub) in zip(lx, loop):
        body.append(card(x, pg_y + 8, w, 104, title, sub, title_size=19, sub_size=14))
    body.append(arrow([(lx[0] + w / 2, r2 + h + 4), (lx[0] + w / 2, pg_y + 2)], width=2))
    body.append(pill(lx[0] + w / 2 + 2, (r2 + h + pg_y) / 2 + 4, "人工定论", size=13))
    for a, b in zip(lx, lx[1:]):
        body.append(arrow([(a + w + 4, pg_y + 60), (b - 6, pg_y + 60)], width=2))
    # gold labels feed back into the risk thresholds and stage coefficients right above
    body.append(arrow([(lx[2] + w / 2, pg_y + 4), (lx[2] + w / 2, r2 + h + 6)],
                      color=AMBER, width=2, dashed=True, marker="arrowAmber"))
    body.append(pill(lx[2] + w / 2 + 2, (r2 + h + pg_y) / 2 + 4, "持续迭代 · 标定阈值与系数", color=AMBER, size=13))
    # legend
    lgx = 80
    body.append(f'<line x1="{lgx}" y1="1046" x2="{lgx + 46}" y2="1046" stroke="{CYAN}" stroke-width="2.2" marker-end="url(#arrow)"/>')
    body.append(text(lgx + 58, 1051, "主数据流", 15, MUTED))
    body.append(f'<line x1="{lgx + 160}" y1="1046" x2="{lgx + 206}" y2="1046" stroke="{GREEN}" stroke-width="2.2" marker-end="url(#arrowGreen)"/>')
    body.append(text(lgx + 218, 1051, "语义向量 / Agent", 15, MUTED))
    body.append(f'<line x1="{lgx + 370}" y1="1046" x2="{lgx + 416}" y2="1046" stroke="{AMBER}" stroke-width="2.2" stroke-dasharray="9 7" marker-end="url(#arrowAmber)"/>')
    body.append(text(lgx + 428, 1051, "迭代回流", 15, MUTED))
    body.append(text(lgx + 560, 1051, "★ 差异化引擎", 15, AMBER_SOFT))
    body.append(text(1840, 1051, "数字口径 2026-10-04", 14, DIM, anchor="end"))
    return frame("智能研判引擎 · 技术链路", "一条报道从入库到预警：抽取 → 召回 → 核验 → 可信 → 聚合 → 议题 → 风险 → 预警，人工定论回流",
                 "\n".join(body), "研判引擎链路：报道经抽取、双路召回、四值核验、可信评估、聚合去重、议题台账、阶段风险后生成预警，人工复核回流")


# ---------------------------------------------------------------------------
# Figure 3 — technology stack and deployment
# ---------------------------------------------------------------------------

def zone(x, y, w, h, title, tag, color):
    return "\n".join([
        rect(x, y, w, h, fill=PANEL, stroke=color, rx=18, sw=1.4, extra='stroke-opacity="0.55"'),
        rect(x + 20, y - 16, len(title) * 21 + 40, 32, fill=BG, stroke=color, rx=16, sw=1.4),
        text(x + 40, y + 7, title, 18, color, "bold"),
        text(x + w - 22, y + 34, tag, 13, DIM, "bold", "end", spacing=2),
    ])


def figure3() -> str:
    body = []
    top, height = 168, 690
    # zones
    body.append(zone(40, top, 470, height, "离线算力区 · GPU 服务器", "OFFLINE GPU", GREEN))
    body.append(zone(550, top, 640, height, "数据处理区 · 处理机", "PIPELINE", CYAN))
    body.append(zone(1230, top, 650, height, "在线服务区 · 部署服务器", "ONLINE", AMBER))
    # GPU zone
    body.append(node(80, 230, 390, 150, None, "Tesla V100 · 32GB", "PyTorch CUDA · fp16 推理\n构建期余 10.8GB 显存", "161.6 秒完成全量向量化", accent=GREEN))
    body.append(node(80, 420, 390, 150, None, "BGE-M3 多语言向量", "1,024 维 · 中英繁跨语言\n93,209 段 + 3,922 条主张", "每条主张 Top-20 近邻", accent=GREEN))
    body.append(node(80, 640, 390, 180, None, "近邻文件 1.7MB", "只含编号、不含文本\n段落指纹 + SHA-256 校验\n不符即拒绝导入", "保密：模型与向量不出 GPU 机", accent=GREEN))
    body.append(arrow([(275, 384), (275, 416)], color=GREEN, width=2, marker="arrowGreen"))
    body.append(arrow([(275, 574), (275, 636)], color=GREEN, width=2, marker="arrowGreen"))
    # pipeline zone
    body.append(node(590, 230, 270, 150, None, "合规采集调度器", "25 个站点适配器\nrobots · 限速 · 熔断", "55 信源 · 4,339 版本"))
    body.append(node(890, 230, 270, 150, None, "研判处理器", "Python 3.12\n抽取 · 核验 · 聚合 · 预警", "3,922 条主张逐条核验"))
    body.append(node(590, 420, 570, 150, None, "OpenAI 兼容模型网关", "DeepSeek-V3.2（长文档）· 通义千问 qwen-plus（核验）\nDeepSeek-V4-Pro（盲判预标注）· Kimi-K3（研判助手）", "JSON Schema 约束 · 提示词版本化"))
    body.append(node(590, 640, 570, 180, None, "SQLite 分析库 + FTS5 索引", "处理层结论 · 93,209 段证据 · 向量近邻\n一致性快照（sqlite backup API）", "286MB · processor.verify 0 错误"))
    body.append(arrow([(864, 305), (886, 305)], width=2))
    body.append(arrow([(1025, 384), (1025, 416)], width=2))
    body.append(pill(1090, 400, "结构化调用", size=12))
    body.append(arrow([(725, 574), (725, 636)], width=2))
    body.append(pill(725, 605, "结论写入", size=12))
    # gpu <-> pipeline
    # export: paragraph and claim text leave the analysis database for the GPU box, nothing else does
    body.append(arrow([(586, 690), (530, 690), (530, 495), (474, 495)], color=GREEN, width=2, dashed=True, marker="arrowGreen"))
    body.append(pill(530, 600, "导出 39MB", color=GREEN, size=12))
    body.append(arrow([(474, 760), (586, 760)], color=GREEN, width=2, marker="arrowGreen"))
    body.append(pill(530, 728, "导入校验", color=GREEN, size=12))
    # online zone
    body.append(node(1270, 230, 280, 150, None, "浏览器", "Vue 3 · Naive UI\nECharts 6 · Vite 6", "31 页面 · 三主题 · 窄屏"))
    body.append(node(1570, 230, 270, 150, None, "公网 HTTPS", "域名访问 · TLS\n反向代理接入应用", "TLS 加密传输", accent=AMBER))
    body.append(node(1270, 420, 570, 150, None, "FastAPI · Uvicorn", "95 个接口 · 默认全量鉴权 · 四级 RBAC · 只追加审计\nGZip · SSE 流式 · 错误不泄露堆栈", "页面接口 P95 57.6ms", accent=AMBER))
    body.append(node(1270, 640, 570, 180, None, "SQLite 只读挂载 + 人工决策库", "分析库 mode=ro：应用层物理上改不了证据\n复核 · 处置 · 账号 · 审计独立成表，重跑不覆盖", "账号与处置记录随库保留", accent=AMBER))
    body.append(arrow([(1554, 305), (1566, 305)], color=AMBER, width=2, marker="arrowAmber"))
    body.append(arrow([(1705, 384), (1705, 416)], color=AMBER, width=2, marker="arrowAmber"))
    body.append(pill(1760, 400, "HTTPS", color=AMBER, size=12))
    body.append(arrow([(1555, 574), (1555, 636)], color=AMBER, width=2, marker="arrowAmber"))
    body.append(pill(1630, 605, "只读查询", color=AMBER, size=12))
    # release: pipeline -> online
    body.append(arrow([(1164, 760), (1266, 760)], width=2.4))
    body.append(pill(1215, 728, "发布包", size=12))
    # assistant -> gateway
    body.append(arrow([(1266, 495), (1164, 495)], color=GREEN, width=2, dashed=True, marker="arrowGreen"))
    body.append(pill(1215, 528, "助手查证", color=GREEN, size=12))
    # engineering base
    base_y = 892
    body.append(rect(40, base_y, 1840, 132, fill=PANEL, stroke=BAND_EDGE, rx=18, sw=1.2))
    body.append(text(68, base_y + 36, "工程底座", 20, TEXT, "bold"))
    body.append(text(68, base_y + 60, "ENGINEERING", 12, DIM, "bold", spacing=2))
    items = [("Docker 多阶段构建", "minimal / full 两档 · 非 root · 健康检查"),
             ("一键发布", "部署包 + git bundle + SHA256SUMS"),
             ("自动化测试", "后端 1,797 · 前端 403 全绿"),
             ("性能", "页面 P95 57.6ms · 检索 P95 159.7ms"),
             ("可回滚", "旧版前后端保留 · 库备份")]
    body.append(row(240, 1860, base_y + 20, 92, items, gap=14, title_size=18, sub_size=13))
    return frame("技术栈与部署架构", "离线算力 · 数据处理 · 在线服务三区分离：重计算放 GPU，结论随发布包上线，线上只读",
                 "\n".join(body), "部署架构：GPU 离线向量化、处理机生成结论、部署服务器只读提供在线服务，底部为工程底座")


def main() -> None:
    for name, build in (("fig1-architecture", figure1), ("fig2-engine", figure2), ("fig3-deployment", figure3)):
        path = OUT / f"{name}.svg"
        path.write_text(build(), encoding="utf-8")
        print("wrote", path)


if __name__ == "__main__":
    main()
