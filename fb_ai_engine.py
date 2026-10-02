# -*- coding: utf-8 -*-
"""
AI 广告决策引擎（纯逻辑层，无 GUI 依赖）
链路：数据快照 -> prompt -> 9router LLM -> 结构化决策

判断权完全交给 LLM（2026-09-30 用户决定撤掉规则层）：
  - 第 1 层「规则预筛」（原 _prescreen.py 的 rule_score 硬阈值打标）已撤
  - 第 3 层「输出护栏」（原 apply_guardrails 的预算幅度夹取）已改开关，默认关闭
只保留工程必需项：同对象同轮去重、建议条数截断。

2026-10-02 补回两条「钱相关的硬线」（不是恢复老的阈值打标，是被实测打回来的一部分）：
  - 关停护栏 enforce_risk_guardrails：止损线（净≤0 强制停）+ 禁杀线（净>0 禁止停）
  - 加预算提名：规则提名、人拍板（**不默认替 AI 做决定**）
  原因见该函数上方注释：AI 关停漏杀 9 条、误杀 6 条（误杀代价是漏杀的 11 倍），
  而把这些道理写进提示词会让总账更差 —— 硬线只能放代码里。
"""
import json
import os
import time
import re
from datetime import datetime, timedelta

# ---------- 规则层开关（默认全撤，AI 全权判断）----------
ENABLE_BUDGET_CAP = False   # 第3层护栏：是否把预算调幅夹取到 max_budget_change_pct。False=不夹，AI 说多少就是多少

# ---------- 关停护栏 & 加预算提名（2026-10-02 新增）----------
# 起因：在 v5 模拟数据（100 广告）上实测，AI 的关停判断有两处系统性偏差：
#   · 漏杀 9 条 —— 真值「暂停」却给 observe。它们唯一共性是「亏得不够多」（亏 -35 vs 判停组的 -79），
#     说明 AI 有一条未言明的「亏得够多才停」的线，从没写进规则。
#   · 误杀 6 条 —— 其中 3 条可见窗口在赚钱。代价 $715，是漏杀代价（$66）的 11 倍。
# 实测四方对照（100 条 · 后 5 天净收益口径）：
#   ① 纯 AI 57 分（漏杀 -$66 / 误杀 -$715）
#   ② AI + 提示词护栏 63 分（漏杀 $0 / 误杀 -$824，**更糟**）→ 给 AI 讲道理 = 放大它原有的毛病
#   ③ AI + 代码护栏 68 分（漏杀 $0 / 误杀 +$2）           ← 本模块实现的就是这个
#   ④ 纯规则 80 分
# 结论：AI 是好审查员，不是好决策者。钱相关的硬线放代码里，不放提示词里。
#
# ⚠️ 口径与适用边界（换数据必须重算，别当成普适真理）：
#   · 「净」= 窗口内 Σ购买价值 − Σ花费（**不含货成本**），只用来判断这笔广告投得值不值。
#   · 止损线 / 禁杀线 / 提名阈值全部是在**本机 v5 模拟数据**上拟合的。那份数据的规律由机制生成，
#     规则天然占优；真实账户形态更杂，这 12 分优势不能直接搬过去。阈值先用着，真实数据到手再校。
ENABLE_KILL_GUARDRAIL = True     # 关停护栏（止损线 + 禁杀线）
ENABLE_BUDGET_NOMINATION = True  # 加预算规则提名（**只提名，不改 AI 的动作**）

KILL_STOP_NET = 0.0       # 止损线：可见窗口净额 ≤ 此值 → 强制暂停（这段没赚钱，放量救不了亏损）
NOMINATE_FILL_RATE = 0.90  # 提名线①：顶格率 ≥ 此值 —— 预算卡住了它，不是它跑不动
NOMINATE_FREQ_MAX = 1.35   # 提名线②（**旧值，已被 ③ 取代，保留仅为兼容旧 config**）
NOMINATE_FREQ_SATURATED = 3.5   # 提名线②：末段频次 < 此值才算「受众还没看腻」
                                #   🔴 原值 1.35 是在 v5 数据上拟合的，换数据立刻失真 ——
                                #   双盲实测：频次 2.4（远未饱和、加预算后 ROAS 反而升到 4.64）的那条
                                #   提名数 = 0。**饱和线是有行业共识的量（3~3.5），拟合值没有。**
NOMINATE_FREQ_GROWTH_MAX = 0.5  # 提名线③：频次增幅（末−首）≤ 此值 —— 没在快速堆高
NOMINATE_MIN_DAYS = 3      # 样本下限：窗口不足这么多天不提名（少样本的高 ROAS 是噪声）

# ---------- 护栏前置条件（2026-10-03 双盲实测后加） ----------
# 🔴 为什么需要：止损线只看 `net = 购买价值 − 花费`，**不看口径列、不看样本量、不看亏损是「哪一环」造成的**。
#   双盲实测里它把两条本不该关的广告一刀切停：
#     · 优化目标是 add_to_cart 的那条（加购 280 个、$2.14/个，加购极健康）
#     · 只点了 50 次、零单的那条（零单在这个样本量下是正常现象）
#   加上 T-05/T-06 两轮都被误判关停 ⇒ 误杀率 40% / 60%。
#   ⇒ 命中任一条时，护栏**不越权强制**，把判断交回 AI（并在 reason 里写明是哪一条）。
#   ⚠️ 护栏**只管「AI 没判停但净额为负」这个方向**；AI 自己判了 pause 的，护栏本来就不动 ——
#   所以前置条件不会「放过该关的广告」，它只阻止「护栏替 AI 做它不该做的决定」。
STOP_MIN_CLICKS = 100         # 点击数 < 此值 ⇒ 零单是样本不足，不构成止损依据
                                #   🔴 原先取 30，双盲重放**当场翻车**：T-02 有 50 次点击、0 单，
                                #   AI 判的是「观察」（理由：累计到 100 次点击仍 0 单才关），
                                #   结果被护栏强制暂停 ⇒ 误杀从 2/40% 涨到 3/60%。
                                #   门槛必须定在「这个样本量下 0 单才有意义」的位置，不是随手取整。
STOP_MIN_LOSS_ABS = None      # 🔴 **已否决、不启用**（保留常量名以说明为什么不加）。
                                #   「亏 50 美元就关」是噪音；但「亏 50 美元就永远不管」是更大的错 ——
                                #   每天只花 $5、每天亏 $3 的广告会因此永远不被止损线关掉。
STOP_WATCH_SEC_LOW = 3.0       # 平均观看 < 此秒 ⇒ 只被开头钩住（素材问题，不是流量问题）
STOP_REPEAT_CLICK_HIGH = 0.30  # 重复点击占比 > 此值 ⇒ 少数人反复点（落地页/信任问题）
STOP_ACTIVE_STATUS = ('ACTIVE',)   # 只有真正在投的才谈得上「止损」

# ---------- 配置 ----------
AI_CONFIG_DEFAULTS = {
    "base_url": "",          # 例如 https://xxx.9router.com/v1（OpenAI 兼容）
    "api_key": "",           # 9router key（建议放环境变量 AI_API_KEY，不放代码里）
    "model": "deepseek-v4-flash",
    "interval_minutes": 3,   # 判断频率（用户拍板：3分钟）
    "max_budget_change_pct": 20,     # 仅供参考值：写进 prompt 让 AI 知道合理量级，不做强制夹取
    "suggestion_limit": 5,           # 单轮最多返回建议数
    "batch_size": 10,                # 每批投喂条数（快照含逐日明细后单条约 2.3K 字符）
    "concurrent_workers": 3,         # 并发投喂路数；1 = 退回串行
    "stagger_seconds": 4,            # 每轮之间的错峰间隔（实测 4s 时 429 归零）
}


# ---------- 快照 ----------
def _compute_age_hours(c):
    """从 start_time 计算系列上线时长（小时），无 start_time 返回 None"""
    st = c.get('start_time')
    if not st:
        return None
    try:
        # 兼容 '2026-09-17T00:00:00+0800'（+0800 是旧式时区格式，先补冒号转标准 ISO）
        s = st.strip()
        if 'T' in s:
            if s.endswith('+0800'):
                s = s[:-5] + '+08:00'
            dt = datetime.fromisoformat(s)
            now = datetime.now(dt.tzinfo)  # 与解析结果对齐时区，避免 naive/aware 相减
        else:
            dt = datetime.strptime(s[:19], '%Y-%m-%d %H:%M:%S')
            now = datetime.now()
        age = (now - dt).total_seconds() / 3600
        return round(age, 1) if age >= 0 else None
    except (ValueError, TypeError):
        return None


def _sample_stats(days):
    """
    由代码算好「样本量」，塞进快照 —— 不让模型自己数。

    2026-10-01 修正起因：prompt 里要求「给 increase_budget 必须写出样本量」，
    模型照写了，但**数错了** —— 把「7天里出6单」写成「6天有单共6单」，
    9 轮里反复这么写。它自报的样本量本身就是错的，整个推理起点就歪了。
    能算的东西就该由代码算：模型负责判断，不负责算术。

    返回 dict（无逐日明细时返回 None）：
      days                    窗口天数（有数据的天数）
      days_with_orders        出单天数
      orders_total            窗口内总单数
      days_spend_no_order     花了钱但零单的天数
      last3_days              最近3天实际天数（窗口不足3天时等于窗口天数）
      last3_spend/last3_orders/last3_roas   最近3天合计
      longest_zero_order_streak  最长「连续有花费且零单」天数
      first_order_date        首次出单日期
    """
    n = len(days)
    if n == 0:
        return None
    orders = days_with_orders = days_spend_no_order = 0
    longest = cur = 0
    first_order_date = ''
    for d in days:
        pur = int(d.get('purchase') or 0)
        sp = float(d.get('spend') or 0)
        orders += pur
        if pur > 0:
            days_with_orders += 1
            cur = 0
            if not first_order_date:
                first_order_date = d.get('date', '')
        else:
            if sp > 0:
                days_spend_no_order += 1
                cur += 1
                if cur > longest:
                    longest = cur
            else:
                cur = 0        # 当天没投，不算进「连续烧钱零单」
    last3 = days[-3:]
    l3s = round(sum(float(d.get('spend') or 0) for d in last3), 2)
    l3o = sum(int(d.get('purchase') or 0) for d in last3)
    l3v = sum(float(d.get('purchase_value') or 0) for d in last3)
    out = {
        "days": n,
        "days_with_orders": days_with_orders,
        "orders_total": orders,
        "days_spend_no_order": days_spend_no_order,
        "last3_days": len(last3),
        "last3_spend": l3s,
        "last3_orders": l3o,
        "last3_roas": round(l3v / l3s, 2) if l3s > 0 else 0.0,
        "longest_zero_order_streak": longest,
    }
    if first_order_date:
        out["first_order_date"] = first_order_date
    return out


def _net_view(days, daily_budget=None):
    """
    可见窗口的「钱账」—— 由代码算好，塞进快照，也是关停护栏的输入。

    为什么单独立一个：判断「关停」这件事，看的是**钱**（净额），不是**单**（购买次数）。
    2026-10-02 实测（v5 数据）：「连着两天零单就不加」命中率 62%，而「看这几天是赚还是亏」79%。
    更直接的一条：某广告 5 天 274 次点击只出 3 单（转化率 1.095%），
    **按这个转化率连着两天零单的自然概率是 29.9%** —— 零单不是信号，是运气。

    返回 dict（无逐日明细时返回 None）：
      days/spend/revenue/net/roas      窗口合计与净额（net = revenue − spend）
      active_days                      有花费的天数
      fill_rate                        顶格率 = 花费达到日预算 95% 的天数 / 有花费的天数
      freq_first/freq_last             首/末个有花费天的频次（看受众消耗速度）
      cum_net_min                      累计净额的最低点（最深亏到多少）
      cum_negative_since               累计净额首次转负的日期（「该在哪天止损」的锚点）
    """
    n = len(days)
    if n == 0:
        return None
    spend_total = revenue_total = 0.0
    active = full_days = 0
    cum = worst = 0.0
    neg_since = None
    freq_first = freq_last = None
    for d in days:
        sp = float(d.get('spend') or 0)
        pv = float(d.get('purchase_value') or 0)
        spend_total += sp
        revenue_total += pv
        cum += pv - sp
        if cum < 0 and neg_since is None:
            neg_since = d.get('date', '')
        if cum < worst:
            worst = cum
        if sp > 0:
            active += 1
            bud = float(daily_budget or 0)
            if bud > 0 and sp >= 0.95 * bud:
                full_days += 1
            f = d.get('frequency')
            if f not in (None, ''):
                try:
                    f = float(f)
                except (TypeError, ValueError):
                    f = None
                if f is not None:
                    if freq_first is None:
                        freq_first = f
                    freq_last = f
    out = {
        "days": n,
        "spend": round(spend_total, 2),
        "revenue": round(revenue_total, 2),
        "net": round(cum, 2),
        "roas": round(revenue_total / spend_total, 2) if spend_total > 0 else 0.0,
        "active_days": active,
        "cum_net_min": round(worst, 2),
    }
    if active > 0:
        out["fill_rate"] = round(full_days / active, 3)
    if freq_first is not None:
        out["freq_first"] = round(freq_first, 3)
        out["freq_last"] = round(freq_last, 3)
    if neg_since:
        out["cum_negative_since"] = neg_since
    return out


def make_snapshot(campaigns_insights):
    """
    把系列洞察列表整理成给 LLM 的结构化摘要。
    campaigns_insights: [{'id','name','status','spend','impressions','clicks',
                          'cpc','cpm','purchase','purchase_value','cost_per_purchase', 'start_time'?}, ...]
    """
    rows = []
    for c in campaigns_insights:
        spend = float(c.get('spend') or 0)
        purchase_value = float(c.get('purchase_value') or 0)
        roas = (purchase_value / spend) if spend > 0 else 0.0
        impressions = int(c.get('impressions') or 0)
        clicks = int(c.get('clicks') or 0)
        add_to_cart = int(c.get('add_to_cart') or 0)
        initiate_checkout = int(c.get('initiate_checkout') or 0)
        add_payment_info = int(c.get('add_payment_info') or 0)
        view_content = int(c.get('view_content') or 0)
        row = {
            "id": c.get('id'),
            "name": c.get('name', ''),
            "status": c.get('status', ''),
            "objective": c.get('objective', ''),   # 广告目标（OUTCOME_SALES/OUTCOME_ENGAGEMENT等）
            "budget_type": c.get('budget_type', ''),   # ABO（广告组级预算）/ CBO（系列级预算）
            "campaign_daily_budget": c.get('campaign_daily_budget'),  # 系列日预算（CBO 时有效）
            "adset_daily_budget": c.get('adset_daily_budget'),        # 广告组日预算（ABO 时有效）
            "ad_type": c.get('ad_type', ''),       # 广告类型：基础 / ASC / DPA
            "spend": round(spend, 2),
            "impressions": impressions,
            "reach": int(c.get('reach') or 0),     # 覆盖人数
            "frequency": round(float(c.get('frequency') or 0), 2),    # 频次（展示/覆盖）
            "clicks": clicks,
            "ctr": round((clicks / impressions * 100) if impressions > 0 else 0, 2),   # 点击率 %
            "cpc": round(float(c.get('cpc') or 0), 2),
            "cpm": round(float(c.get('cpm') or 0), 2),
            # 转化漏斗（销量广告重点）
            "add_to_cart": add_to_cart,
            "initiate_checkout": initiate_checkout,
            "add_payment_info": add_payment_info,
            "view_content": view_content,
            "purchase": int(c.get('purchase') or 0),
            "purchase_value": round(purchase_value, 2),
            "cost_per_purchase": round(float(c.get('cost_per_purchase') or 0), 2),
            "roas": round(roas, 2),
            # 漏斗转化率（购买/加购）
            "cart_to_purchase_rate": round((int(c.get('purchase') or 0) / add_to_cart * 100) if add_to_cart > 0 else 0, 2),
        }
        if c.get('age_hours') is not None:
            row['age_hours'] = c.get('age_hours')
        else:
            age = _compute_age_hours(c)
            if age is not None:
                row['age_hours'] = age
        # 每日数据（广告级「完整生命周期」判断用）：**完整透传**，不再精简
        # 2026-09-30 修正：旧版只保留 date/spend/purchase 三项，AI 每天只看到「花了多少、出几单」
        #   两条线，看不到逐日点击/加购/结账/支付/覆盖/频次的波动 —— 无法判断「转化死在哪一环」、
        #   「当天是素材崩了还是时段没到」。现在按天给全字段。
        # 控体积规则：有花费的天输出全部转化字段（0 也要给，因为「有花费但0加购」本身就是信号）；
        #   零花费的天只留 date+spend（当天没投，其余全无意义）。
        if c.get('daily_spend') is not None:
            days = []
            for d in c['daily_spend']:
                spend = round(float(d.get('spend') or 0), 2)
                it = {'date': d.get('date', ''), 'spend': spend}
                if spend > 0:
                    imp = int(d.get('impressions') or 0)
                    clk = int(d.get('clicks') or d.get('inline_link_clicks') or 0)
                    if imp:
                        it['impressions'] = imp
                        it['ctr'] = round(clk / imp * 100, 2) if clk else 0.0
                        it['cpm'] = round(spend * 1000 / imp, 2)
                    if clk:
                        it['clicks'] = clk
                    for src, dst in (('reach', 'reach'), ('frequency', 'frequency'),
                                     ('add_to_cart', 'add_to_cart'),
                                     ('initiate_checkout', 'initiate_checkout'),
                                     ('add_payment_info', 'add_payment_info'),
                                     ('view_content', 'view_content')):
                        v = d.get(src)
                        if v not in (None, ''):
                            it[dst] = round(float(v), 2) if dst == 'frequency' else int(float(v))
                    pur = int(d.get('purchase') or 0)
                    pv = float(d.get('purchase_value') or 0)
                    it['purchase'] = pur
                    it['purchase_value'] = round(pv, 2)
                    it['roas'] = round(pv / spend, 2) if spend > 0 else 0.0
                days.append(it)
            row['daily_spend'] = days
            # 样本量由代码算好（模型会数错，见 _sample_stats 注释）
            _smp = _sample_stats(days)
            if _smp:
                row['sample'] = _smp
            # 钱账也由代码算好（护栏的输入；顺带让模型不必自己从逐日明细里加总）
            _nv = _net_view(days, row.get('adset_daily_budget') or row.get('campaign_daily_budget'))
            if _nv:
                row['net_view'] = _nv
        # ---- P0/P1：口径与竞争对照（2026-10-02）----
        # 只带非空项：缺了就不出现在快照里，prompt 会按「该字段未知」处理。
        # 这三类字段不参与任何计算，只作为**读数的前提**进入 prompt：
        #   attribution        归因窗口  → 跨窗口的 ROAS/购买不可比
        #   optimization_event 优化目标  → 优化加购的广告不因「购买少」被关
        #   rankings           竞争排名  → 唯一的外部对照（缺 = Meta 没给，≠ 差）
        if c.get('attribution'):
            row['attribution'] = c.get('attribution')
        if c.get('optimization_event'):
            row['optimization_event'] = c.get('optimization_event')
        if c.get('rankings'):
            row['rankings'] = c.get('rankings')
        if c.get('end_date'):
            row['end_date'] = c.get('end_date')
        if c.get('cost_per_thruplay') is not None:
            row['cost_per_thruplay'] = c.get('cost_per_thruplay')
        # ---- P1b：补充信号（2026-10-02）----
        #   video_avg_watch_sec 观众平均看了几秒 → 3 秒播放数高但时长很短 = 只是被前 3 秒钩住
        #   repeat_click_ratio  非独立点击数占比 → 高 = **少数人反复点却不买**，
        #                       问题在落地页/信任，不在素材曝光；这与「很多人感兴趣」是两回事
        # 两者都带支撑天数（video_watch_days / repeat_click_days）：实测这两列只在真投放的
        # 天有值，均值可能只由 1 天算出，不写天数等于让模型把 1 天当整窗口。
        if c.get('video_avg_watch_sec') is not None:
            row['video_avg_watch_sec'] = c.get('video_avg_watch_sec')
            row['video_watch_days'] = c.get('video_watch_days')
        if c.get('repeat_click_ratio') is not None:
            row['repeat_click_ratio'] = c.get('repeat_click_ratio')
            row['repeat_click_days'] = c.get('repeat_click_days')
        rows.append(row)
    return rows


def _flag_section(snapshot):
    """按本批快照的实际内容，**逐条点名**那些容易被忽略的强信号。

    🔴 为什么不只写在字段说明里（2026-10-03 双盲实测）：
       `repeat_click_ratio` 的规则早就写进 prompt 了，但两轮盲测**没有一条提到它** ——
       28 列里它不显眼，而「918 次点击 0 单」是个强得多的信号，直接把它压过去了。
       **信号存在 ≠ 会被用。** 埋得再深也是埋着。
       ⇒ 命中阈值的条目，在这里按名字单独点名，要求模型在 reason 里正面回应。
    """
    RC = 0.30
    SEC = 3.0
    rows = []
    for r in (snapshot or []):
        nm = r.get('name') or r.get('id') or '(无名)'
        rc = r.get('repeat_click_ratio')
        sec = r.get('video_avg_watch_sec')
        notes = []
        if rc is not None:
            try:
                if float(rc) > RC:
                    notes.append('非独立点击占比 %.0f%%（>%.0f%%）⇒ 少数人反复点，'
                                 '**病根在落地页/价格/信任，不在曝光**'
                                 % (float(rc) * 100.0, RC * 100.0))
            except (TypeError, ValueError):
                pass
        if sec is not None:
            try:
                if 0 < float(sec) < SEC:
                    notes.append('平均观看仅 %.1f 秒（<%.1f 秒）⇒ **素材只钩住了开头**，'
                                 '该换素材/重写前 3 秒，不是调出价或砍预算' % (float(sec), SEC))
            except (TypeError, ValueError):
                pass
        if notes:
            rows.append('  · **%s**：%s' % (nm, '；'.join(notes)))
    if not rows:
        return ''
    return ('\n【本批里这几条有强信号 —— 必须在 reason 里正面回应，不许当作没看见】\n'
            + '\n'.join(rows)
            + '\n  → 出现这些信号时，**先怀疑某一环坏了，而不是先怀疑「没人要」**。\n')


def build_prompt(snapshot, config, window=None):
    """
    构造发给 LLM 的 prompt。config 是完整配置（含业务规则阈值）。
    要求模型只输出 JSON，便于程序解析。
    业务边界：不限制"在跑"，多日/单日数据都判断；有历史看多日变化，只有单日看单日表现。
    判断本身交给模型（人肉式投放经验）。

    window: 可选 (start_date, end_date) 字符串元组，覆盖 prompt 里的口径声明。
            不传 = 按 config['lookback_days'] 从今天往前推（生产行为）。
            传 = 用给定区间（拿上传的历史文件测 AI 时用 —— 模拟数据是 09-23~09-29，
                 若还按「今天往前推 7 天」声明，prompt 说的窗口就和数据里的日期对不上，
                 模型会拿一个错误的"今天"去判断趋势）。
    """
    # 口径声明要用到（2026-09-30）：模型必须知道这些汇总是「最近 N 天」而不是全历史累计。
    # 不写清楚时模型会把顶层 spend 当成累计/今天的数，理由里就会出现与界面显示对不上的花费。
    try:
        lookback_days = int(config.get('lookback_days', 7) or 7)
    except (TypeError, ValueError):
        lookback_days = 7
    if window and len(window) >= 2 and window[0] and window[1]:
        window_start = str(window[0])
        today_str = str(window[1])
        try:
            d0 = datetime.strptime(window_start, '%Y-%m-%d')
            d1 = datetime.strptime(today_str, '%Y-%m-%d')
            lookback_days = max(1, (d1 - d0).days + 1)
        except ValueError:
            pass   # 日期格式不是 YYYY-MM-DD 就只覆盖声明文字，天数保持原值
    else:
        today_str = datetime.now().strftime('%Y-%m-%d')
        window_start = (datetime.now() - timedelta(days=lookback_days - 1)).strftime('%Y-%m-%d')

    keys = ('max_budget_change_pct', 'business_rules')
    rules = {k: config.get(k) for k in keys if k in config}
    rules_text = json.dumps(rules, ensure_ascii=False, indent=2)
    snap_text = json.dumps(snapshot, ensure_ascii=False, indent=2)
    n = len(snapshot)
    # 数据缺口声明（2026-09-30）：用上传文件测 AI 时，导出表可能**结构性缺列**（实测 700 行
    # 那份没有「内容查看」列 → view_content 全是 0）。若不说，模型会把「缺列」读成
    # 「真实的零内容查看」，进而把「0 内容查看却有加购」当成数据异常 → 判断整体偏保守（实测召回率掉一半）。
    # 缺列是**数据事实**，必须显式告诉模型，而不是让它自己猜。
    note = str(config.get('data_note') or '').strip()
    note_text = (f"\n【本批数据的已知缺口 —— 判断时必须考虑，别把「缺列」当成真实的 0】\n{note}\n"
                 if note else "")
    return (
        "直接输出 JSON 数组，不要任何前缀文字、解释或思考过程。\n"
        f"你是资深 Facebook 广告投放优化师。**今天是 {today_str}**。下面是 {n} 条广告的数据快照：每条含汇总指标"
        "（spend / roas / 漏斗各环节 / ctr / cpm / frequency / 预算类型等）、**程序算好的样本量 sample**、"
        "**口径与对照字段（status 投放状态 / attribution 归因窗口 / optimization_event 优化目标 / rankings 竞争排名）**、"
        "**补充信号（video_avg_watch_sec 平均观看秒数 / repeat_click_ratio 重复点击占比）**、"
        "与**逐日明细 daily_spend**。\n"
        "【数据口径 —— 必须先读懂再判断】\n"
        f"  · 时间窗：**{window_start} ~ {today_str}**（最近 {lookback_days} 天）。每条广告顶层的 "
        f"spend / impressions / purchase / purchase_value / roas / ctr / cpm / frequency 都是**这 {lookback_days} 天的合计**，"
        "既不是全历史累计、也不是今天的数字。reason 里提到花费时必须按这个口径说。\n"
        "  · daily_spend 覆盖窗口内**每一天**（自该广告创建日起），**包括没花钱的天**："
        "某天写成 `date + spend:0` 就是**当天真实零花费**（被暂停 / 预算耗尽 / 没跑起来 / 审核没过），"
        "**不是数据缺失**，必须当成事实用。\n"
        "  · 判断动作看**最近几天**的走势（爬升 / 持平 / 断崖），不要被窗口合计掩盖近期恶化。\n"
        "  · 若 daily_spend 最后一天（即今天）spend 为 0：说明它今天没在花钱 —— 此时**不需要 pause**"
        "（它本来就没花），给 observe 即可，但要在 reason 里点出「从哪天起停了」。\n"
        "  · **status 非 ACTIVE（如 not_delivering / 已暂停 / 已结束）= 现在根本没在投**："
        "它没有花钱、也拿不到新数据。此时**不得给 increase_budget**（花钱动作没有承接对象），"
        "给 observe 并在 reason 里点出「当前未投放」。"
        "（这条只约束花钱动作，不影响你如实描述它的历史表现。）\n"
        "  · **attribution = 本批数据采用的归因窗口**（如「点击后 7 天内、浏览后 1 天内」）。"
        "凡是提到 ROAS / 购买数 / 单均成本的横向对比，**都必须限定在同一窗口内**；"
        "若某条的 attribution 与别人不同或缺失，只能各自内部前后比，**不得跨窗口排序**。"
        "（这一列缺失时按「窗口未知」处理，**不要假定**是 7d_click。）\n"
        "  · **optimization_event = 这个广告组优化的到底是哪个事件**（购买 / 加购 / 链接点击…）。"
        "判断的靶子必须与它对齐：优化加购 / 结账 / 链接点击的广告组，购物次数天然少，"
        "**不构成 pause / decrease_budget 的理由** —— 那是目标决定的，不是它跑得差。"
        "（这一列缺失时按「目标未知」处理，退回 objective 字段判断。）\n"
        "  · **rankings（quality 质量 / engagement 互动率 / conversion 转化率）= 和抢同批受众的广告比出来的排名**，"
        "是唯一的外部对照：自身 CTR 3% 说明不了好坏，要看排名档位。"
        "**rankings 缺失或为空 = Meta 没给这项数据（通常因为投放量不够），绝不等于「排名差」，不得据此下结论。**\n"
        "  · **video_avg_watch_sec = 观众平均看了几秒**（附 video_watch_days = 这个均值由几天算出）。"
        "用它区分「被开头钩住」和「真的看进去了」：3 秒播放数好看但平均只有 1~2 秒，"
        "说明素材只赢在开头、后半段留不住人 —— **这属于素材问题，不是出价/预算问题。**\n"
        "    🔴 **出现这个信号时，`decrease_budget` 和 `pause` 都不许给** —— "
        "**不要**用「先停损再换素材」：关停会杀掉这条广告已经积累的受众与学习进度，"
        "重新拉起来要再花一份预算和几天时间；而换素材不花钱、见效更快。"
        "正确动作是 `observe` + 在 reason 里写明「换素材」；"
        "**只有在「换素材这条路也走不通」时才 pause**（例如频次已饱和、或换过素材后加权回收仍为负），"
        "且必须把这两点写出来。\n"
        "天数很少（如只有 1 天）时，这个均值只能当**单日观察**，不许说成「整窗口的平均观看时长」。"
        "（字段不存在 = 没有视频观看数据，不得用 3 秒播放数反推观看深度。）\n"
        "  · **repeat_click_ratio = 非独立点击数占全部点击的比例**（同一个人被重复计数）。"
        "高（>0.3）说明**少数人在反复点**，而不是「很多人感兴趣」—— 这类广告 CTR 往往好看但不下单，"
        "问题在落地页 / 价格 / 信任，**不在素材曝光**。\n"
        "    🔴 **出现这个信号时，`pause` / `decrease_budget` 都必须先回答一个问题："
        "落地页、价格、信任这一环查过了吗？** 回答不出来就不许关停，给 `observe` 并写「需先查落地页」。\n"
        "    🔴 **净额为负不构成关停理由**（这条是硬约束，不是建议）："
        "少数人反复点造成的净亏，**关停只是把同一笔钱换个地方继续亏** —— "
        "钱已经花出去买到了流量，把流量连同落地页一起砍掉，下一周期还得重新买一遍。\n"
        "    ⚠️ 本条曾写成「必须结合 net 的正负再定」—— 那是个**逃生口**：净为负时规则字面上就允许关停，"
        "于是三轮盲测全都「正确地遵守了规则」却仍然误杀。**写规则时留「结合 X 再定」这种尾巴，"
        "等于没写禁令。**\n"
        "  · 🔴 **判「亏」之前必须先判「亏在哪」—— 净亏有两种，处置完全相反**：\n"
        "      (a) **买不到有效流量**：落地页到达率、加购率都正常，就是没人买"
        "（加购率低 + 各环通过率没有异常断层）\n"
        "          → 这是流量白买，**pause / decrease_budget 是对的**。\n"
        "      (b) **流量买到了但接不住**：落地页到达率或加购率**明显偏高/正常**，"
        "但后面某一环通过率**异常低**（如加购→结账、加购→支付），"
        "或存在 video_avg_watch_sec < 3、repeat_click_ratio > 0.3 这类信号\n"
        "          → 这是**某一环坏了**，该做的是修那一环（换素材 / 查结账体验 / 查落地页），\n"
        "            **pause 会把「已经买到的流量」连同它的优化空间一起扔掉**。\n"
        "    判别办法（按顺序看，别只看净额）：落地页到达量 → 加购率 → 逐环通过率 → 观看/重复点击信号。"
        "**如果断层出现在某一环中间，就不是「买不到人」，是「人来了没接住」。**"
        "这种情况下 reason 必须写明「断层在第几环、这一环的通过率是多少」，"
        "而不是只说「亏损所以关停」。\n"
        + _flag_section(snapshot)
        + note_text +
        f"【硬性要求】必须对输入中的**每一条**广告都输出一条结论，共 **{n} 条**，一条不能少、不能合并、不能跳过。\n"
        "  表现正常、不需要动作的也必须输出，action 填 \"observe\"。宁可全给 observe，也绝不能漏掉任何一条 ——"
        "漏掉等于这条广告没被判断过。\n"
        "daily_spend 逐日字段含义（某天缺哪个字段，就代表那天该项为 0；某天只有 date+spend:0 表示当天没投）：\n"
        "  date 日期 / spend 当天花费 / impressions 展示 / clicks 链接点击 / ctr 当天点击率% / cpm 当天千展成本 /\n"
        "  reach 覆盖人数 / frequency 频次 / add_to_cart 加购 / initiate_checkout 结账发起 /\n"
        "  add_payment_info 添加支付信息 / view_content 内容查看 / purchase 购买 / purchase_value 购买价值 / roas 当天ROAS\n"
        "  → 逐日序列就是这条广告的生命周期，要读出：花费在爬还是在跌、哪天出现断崖、\n"
        "    加购→结账→支付→购买哪一环开始断、ctr 是否逐日衰减（素材疲劳）、frequency 是否快速堆高（受众见顶）\n"
        "每条广告顶层的 **sample** 字段是**程序已经算好的样本量，直接引用，绝对不要自己数逐日明细去算**"
        "（自己数会数错）：\n"
        "  days 窗口天数 / days_with_orders 出单天数 / orders_total 总单数 / days_spend_no_order 花了钱但零单的天数 /\n"
        "  last3_days 最近3天天数 / last3_spend 最近3天花费 / last3_orders 最近3天单数 / last3_roas 最近3天ROAS /\n"
        "  longest_zero_order_streak 最长「连续有花费且零单」的天数 / first_order_date 首次出单日期\n"
        "  → 凡提到样本量，**必须用统一句式**：「X天里出Y单（出单天数Z）」——X=days、Y=orders_total、Z=days_with_orders，\n"
        "    数字一律取自 sample，不要自己从 daily_spend 里数。\n"
        "  → **必须单独看最近三天**（last3_*）：最近三天在恶化（单数掉/ROAS 掉/花费断）和最近三天在往上走，\n"
        "    结论完全不同。窗口合计漂亮但最近三天已经垮掉的，**不算好**；窗口合计一般但最近三天明显转好的，要指出来。\n"
        "每条广告顶层的 **net_view** 字段是**程序已经算好的钱账**，直接引用，不要自己从逐日明细加总：\n"
        "  spend 窗口花费 / revenue 窗口回收 / net 净额（=revenue−spend，正=赚、负=亏；**不含商品成本**，\n"
        "  只用来判断这笔广告本身投得值不值）/ roas / active_days 有花费天数 /\n"
        "  fill_rate 顶格率（花费打满日预算的天数占比；接近 1 = 预算卡住了它，不是它跑不动）/ \n"
        "  freq_first、freq_last 首末频次（涨得快 = 受众在见顶，放量空间小）/ cum_net_min 累计净额最低点 /\n"
        "  cum_negative_since 累计净额首次转负的日期（这条广告该止损的锚点）\n"
        "  → **关停这个动作，第一判据是 net_view.net 的正负，不是「有没有出单」**：\n"
        "    net ≤ 0 = 这段没赚钱，放量救不了亏损；反过来 net > 0 **不构成** pause 的理由（那是在赚钱的广告）。\n"
        "  → **「几天零单」本身不是关停依据**：点击量不够时零单会自然发生（实测某广告按它自己的转化率，\n"
        "    连着两天零单的概率就有 29.9%），必须落到 net_view.net 上再下结论。\n"
        "    （以上两条只约束 pause / decrease_budget 的判断，**不改变**下面关于 increase_budget 的样本要求。）\n"
        "  → 🔴 **pause 是不可逆动作，「先停损再修」是个陷阱 —— 请先算这笔账**：\n"
        "    关停 = 丢掉已经积累的受众、权重与学习进度；重新拉起来要**再花一份预算 + 几天时间**，\n"
        "    而这段时间是零收入。**先修漏斗/换素材不花钱、见效更快。**\n"
        "    ⇒ 当 net < 0 时，**先问「亏损是「买不到流量」还是「接不住流量」」（见上面那条亏损归因规则）**：\n"
        "      · 买不到流量（落地页到达与加购都正常、各环没有异常断层）→ pause 是对的\n"
        "      · 接不住流量（某一环通过率异常低，或有观看时长 / 重复点击信号）→ **先修那一环**\n"
        "    ⇒ 只有在 (b) 里也**确实修不动**（已经换过素材/改过落地页，加权回收仍为负）时，pause 才成立，\n"
        "    且 reason 必须写明「已经试过什么、为什么不管用」。**只写「亏损所以关停」是不够的。**\n"
        "判断方法（人肉式投放经验，综合多指标判断，必须遵守）：\n"
        "1. 先看 budget_type 和 ad_type 决定判断框架：\n"
        "   - budget_type=ABO：预算是广告组级（adset_daily_budget），单个广告的预算独立，调整预算直接影响这个广告\n"
        "   - budget_type=CBO：预算是系列级（campaign_daily_budget），系列内广告共享预算，调整系列预算影响整个系列，单广告的增减建议要谨慎（可能只是系统分配问题）\n"
        "   - ad_type=DPA 动态商品广告：按商品维度衡量，看商品点击/购买，不看单广告素材\n"
        "   - ad_type=ASC 或基础：常规判断\n"
        "2. 再看 objective（目标）决定看什么指标：\n"
        "   - OUTCOME_SALES/OUTCOME_CONVERSIONS（销量/转化）广告：看完整转化漏斗（view_content 内容查看 -> add_to_cart 加购 -> initiate_checkout 结账发起 -> add_payment_info 添加支付 -> purchase 购买），以及 ROAS、cost_per_purchase、cart_to_purchase_rate（加购转购买率）、CTR\n"
        "     * 漏斗哪一环掉得厉害（比如加购多但购买少 -> 加购转购买率低，是落地页/价格问题不是广告问题）\n"
        "     * 有加购/结账但没有最终购买：说明流量质量可以，转化环节有问题，先观察或减预算，不急着暂停\n"
        "     * 连加购/点击都没有的高花费广告：流量质量差，考虑暂停\n"
        "   - OUTCOME_ENGAGEMENT（互动）广告：看 CTR、CPM、互动成本（post_engagement 互动数）+ frequency 频次（频次过高=受众疲劳）；不看购买；零购买正常，互动成本合理就继续\n"
        "   - OUTCOME_TRAFFIC（流量）广告：看 CPC、CTR、landing_page_view 落地页查看 + frequency，不看购买\n"
        "   - 无 objective 或未知：按销量广告看待\n"
        "3. 用 reach（覆盖）和 frequency（频次）辅助判断：\n"
        "   - reach 接近 impressions 且 frequency 低（约1）：触达广、单人次曝光少，正常\n"
        "   - frequency 高（>3）：受众重复曝光多，可能有广告疲劳，考虑换素材/人群；转化还好则继续，转化差则警惕\n"
        "4. 时间维度：**以 daily_spend 为准**。逐日看花费曲线（爬升/持平/断崖）、逐日看转化出现的位置（第几天才出第一单）、逐日看 ctr 与 frequency 走势。"
        "**每条都要点出最近三天（sample.last3_*）的方向**：在变好还是变坏。只有 1 天数据则按单日判断并说明数据量不足\n"
        "5. 判断倾向（由你自己权衡，不设硬阈值，但必须遵守下面关于「证据强度」的要求）：\n"
        "   - 连续多日高花费零任何转化（连加购/点击都没有）-> 暂停\n"
        "   - 加购/结账有但购买转化率低 -> 观察或减预算（可能落地页问题）\n"
        "   - 数据太少**且看不出明显亏损** -> 观察（注意：亏钱是明确信号，见本条第 3 项，不能拿「数据少」把它盖过去）\n"
        "   - 加预算是这里**唯一会真花钱**的动作，只有证据足够才给 increase_budget。"
        "「表现好」不等于「稳定」，判「稳定」**以 daily_spend 为准**（数一数有几天出了购买、"
        "有几天 ROAS 明显高于其他天）：\n"
        "     * 「稳定」= **多日方向一致** —— 连续若干天都有转化、ROAS 在同一量级，**不是某一天冲高**。\n"
        "     * **单日高点不是稳定**：若这条广告只有 1 天数据、或整条只成交过一两单，它的高 ROAS "
        "极可能是一两笔大额订单或偶然成交撑起来的 —— 那是噪声，不是投放能力。"
        "此时给 observe，并在 reason 里点明样本不足，**不要给 increase_budget**。\n"
        "     * **凡给 increase_budget，reason 必须写出你依据的样本量 + 最近三天表现**，"
        "格式如「7天里出16单（出单天数6），近三天5单 ROAS 6.8」—— 数字一律取自 sample 字段"
        "（days / orders_total / days_with_orders / last3_orders / last3_roas），**不要自己数**。"
        "写不出样本量，就说明你其实没有把握 —— 那就给 observe。\n"
        "   - **⚠️ 上面这两条「要有样本」「写不出就给 observe」只约束 increase_budget —— 它们只在"
        "「要不要多花钱」这个问题上生效。反过来不成立：判断该不该 pause / decrease_budget 时，"
        "绝对不能拿「样本少 / 数据不足 / 数据量有限」当 observe 的理由。**\n"
        "     亏损趋势本身就是证据：连续多日零购买却持续花钱、ROAS 明显低于 1、花费在爬而转化在断 —— "
        "这些恰恰是样本再多也只会更糟的形态，**该停就停**。对这种广告给 observe，等于批准它继续烧钱。\n"
        "6. 每条广告**恰好一条**建议（不要给同一条广告写两条）\n"
        "7. reason 要具体，说明：目标/预算类型 + 看了哪些指标（逐日走势、漏斗哪一环断、ctr/frequency）+ 结论。"
        "**控制在一句话内（约 60~80 字）** —— 一次要输出很多条，写太长会导致响应被截断、后面的广告拿不到结论\n"
        "8. 【最后强调】输出数组的长度必须**等于输入的广告条数**，一条都不能少\n"
        "只输出 JSON 数组，不要输出任何其他文字，格式：\n"
    ) + (
        '[{"campaign_id":"123","action":"pause|increase_budget|decrease_budget|observe",'
        '"budget_change_pct":20,"reason":"..."}]'
        "\n\n业务规则:\n" + rules_text +
        "\n\n数据快照（共 " + str(n) + " 条，请输出 " + str(n) + " 条结论）:\n" + snap_text
    )


# ---------- LLM 调用（OpenAI 兼容） ----------
def call_llm(base_url, api_key, model, prompt, timeout=60):
    """
    调 9router / OpenAI 兼容 chat/completions，返回模型输出文本。
    失败抛异常（由调用方处理）。
    """
    import requests

    if not base_url or not api_key:
        raise ValueError("未配置 base_url / api_key（AI 决策不可用）")
    url = base_url.rstrip('/') + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个只输出 JSON 的 API 端点。禁止输出任何解释、思考过程、markdown 代码块或额外文字，直接输出合法 JSON 数组。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,   # 低温度，输出稳定
        # ⚠️ 原值 max_tokens=2000 会被思考链吃光：实测 3 条广告 reasoning_tokens 就 4380，
        #    响应全文是推理文字、JSON 从未输出 -> parse_suggestions 恒为 []（即「STDS 返回 0 条建议」）。
        #    修正：上限提到 8000，并用 enable_thinking=False 直接关掉思考链（实测 43.6s -> 9.3s，思考 token 归零）。
        "max_tokens": 8000,
        "enable_thinking": False,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
    # 网关可能不返回 charset，默认会按 ISO-8859-1 解码导致中文乱码：强制 utf-8
    try:
        text = resp.content.decode('utf-8', errors='replace')
    except Exception:
        text = resp.text or ""
    ctype = resp.headers.get('Content-Type', '')
    if 'event-stream' in ctype or text.lstrip().startswith('data:'):
        # 兼容：整包 JSON + data:[DONE] 尾巴 / 标准 SSE 多块 / 纯整包
        chunks = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith('data:'):
                payload = line[5:].strip()
                if payload and payload != '[DONE]':
                    chunks.append(payload)
            elif not line.startswith(':'):
                chunks.append(line)
        for cand in chunks:
            try:
                data = json.loads(cand)
                msg = data["choices"][0]["message"]
                content = msg.get("content") or ""
                if not content:
                    # 偶发：STDS 只输出 reasoning_content（思维链），content 空
                    content = msg.get("reasoning_content") or ""
                if content:
                    return content
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                continue
        raise RuntimeError(f"LLM SSE 响应无法解析: {text[:300]}")
    # 纯 JSON
    data = resp.json()
    try:
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        if not content:
            content = msg.get("reasoning_content") or ""
        if not content:
            raise RuntimeError("LLM 响应 content 为空")
        return content
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"LLM 响应格式异常: {str(data)[:300]}")


# ---------- 解析模型输出 ----------
def _recover_json_array(text):
    """
    当整体 json.loads 失败时，尝试逐条恢复 JSON 数组元素。
    做法：按 ',' 粗切分，逐段尝试 json.loads，能解析的留下。
    返回 list[dict]；一条都没有返回 []。
    """
    items = []
    # 去掉外层 [ ]
    body = text.strip()
    if body.startswith('['):
        body = body[1:]
    if body.endswith(']'):
        body = body[:-1]
    # 按行切（STDS 输出每条对象通常独立成段）
    lines = body.split('\n')
    cur = ''
    for line in lines:
        cur += line + '\n'
        s = cur.strip().rstrip(',')
        if s.startswith('{') and s.endswith('}'):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    items.append(obj)
            except json.JSONDecodeError:
                pass
            cur = ''
    return items


def parse_suggestions(text):
    """
    从模型输出文本中提取建议列表。容忍输出包含 markdown 代码块/多余文字。
    返回 [{campaign_id, action, budget_change_pct, reason}]
    """
    if not text:
        return []
    # 去掉 ```json ... ``` 包裹（用贪婪匹配到最后一个 ]，避免嵌套/换行截断）
    m = re.search(r"```(?:json)?\s*(\[.*\])\s*```", text, re.S)
    if m:
        text = m.group(1)
    text = text.strip()
    # 从后往前找 JSON 数组：STDS 可能在思考过程中先写了几个半成品 JSON，
    # 真正的完整 JSON 数组在最后（用 rfind 抓最后一个 [ 到最后一个 ]）
    start = text.rfind('[')
    end = text.rfind(']')
    if start == -1 or end == -1 or end < start:
        return []
    # 如果最后一个 [ 到 ] 解析失败，回退：从最后一个 [ 往前再找更早的候选
    data = None
    for probe_start in (start, text.rfind('[', 0, start), text.find('[')):
        if probe_start == -1 or probe_start >= end:
            continue
        try:
            data = json.loads(text[probe_start:end + 1])
            if isinstance(data, list):
                break
        except json.JSONDecodeError:
            continue
    if data is None:
        data = _recover_json_array(text[start:end + 1])
    suggestions = []
    # 兼容中英文动作词
    valid_actions = {'pause': 'pause',
                     'increase_budget': 'increase_budget',
                     'decrease_budget': 'decrease_budget',
                     'observe': 'observe',
                     '暂停': 'pause',
                     '加预算': 'increase_budget',
                     '减预算': 'decrease_budget',
                     '观察': 'observe',
                     '不动': 'observe',}
    for item in data:
        if not isinstance(item, dict):
            continue
        cid = str(item.get('campaign_id', '')).strip()
        action = str(item.get('action', '')).strip().lower()
        if not cid or action not in valid_actions:
            continue
        suggestions.append({
            'campaign_id': cid,
            'action': valid_actions[action],
            'budget_change_pct': int(item.get('budget_change_pct') or 0),
            'reason': str(item.get('reason', '')),
        })
    return suggestions


# ---------- 输出护栏（已撤，默认不生效）----------
def apply_guardrails(suggestions, snapshots, config):
    """
    原「第 3 层硬护栏」：把 AI 给出的预算调幅夹取到 max_budget_change_pct 以内。

    2026-09-30 用户决定撤掉规则层、让 AI 全权判断：
      ENABLE_BUDGET_CAP = False（默认）时不做任何幅度夹取，AI 说多少就是多少。

    仍然保留的唯一处理（属工程必需，不是规则判断）：
      - 同一对象同一轮只保留第一条建议（否则同一条广告会出现互相冲突的两条建议）

    如需恢复夹取，把本模块顶部的 ENABLE_BUDGET_CAP 改成 True 即可。
    """
    out = []
    seen = set()
    max_pct = config.get('max_budget_change_pct', 20)

    for s in suggestions:
        cid = str(s['campaign_id'])
        if cid in seen:
            continue
        seen.add(cid)
        if ENABLE_BUDGET_CAP and s['action'] in ('increase_budget', 'decrease_budget'):
            pct = abs(s.get('budget_change_pct') or 0)
            if pct > max_pct:
                s['reason'] = f"{s['reason']}（幅度{pct}%超上限{max_pct}%，已限为{max_pct}%）"
                s['budget_change_pct'] = max_pct if s['action'] == 'increase_budget' else -max_pct
        out.append(s)
    return out


# ---------- 关停护栏 & 加预算提名（2026-10-02）----------
ACTION_CN = {'pause': '暂停', 'increase_budget': '加预算',
             'decrease_budget': '减预算', 'observe': '观察'}
STOP_ACTIONS = ('pause', 'decrease_budget')   # 业务上都是「往下压」，护栏按同一口径处理


def _row_index(snapshots):
    return {str(r.get('id')): r for r in (snapshots or []) if r.get('id') is not None}


def _stop_precondition(row, nv, config):
    """止损线的前置条件。命中任一条 ⇒ **不要替 AI 强制关停**，交回它判断。

    返回 (是否阻止, 命中的条件说明)。没命中返回 (False, '')。

    每条都对应一个实测误杀，不是假想风险（2026-10-03 双盲，8 条造 10 天只给 5 天）：

    1. **不在投** —— status 非 ACTIVE（已暂停/预算耗尽/审核不过）。
       它本来就没花钱，pause 没有对象；这条是最基本的常识，护栏却原来不认。
    2. **优化目标不是购买** —— optimization_event 是 add_to_cart / initiate_checkout 等。
       平台在**为加购找人**，购买数少是目标决定的，不是它跑坏了。
    3. **样本不足** —— 累计点击 < STOP_MIN_CLICKS。
       按它自己的转化率算，期望单数不到 1 ⇒ 零单是**正常现象**，不是止损依据。
    4. **亏损来自「接不住」而不是「买不到」** —— 两个可观测信号：
       · 平均观看 < 3 秒（素材在开头就把人筛掉了）
       · 重复点击占比 > 30%（少数人反复点 = 落地页/价格/信任问题）
       这两种情况下净额为负是**某一环坏了**的表现，该修那一环；直接关停治不好它，
       还会把一个「流量已经买到了」的渠道砍掉。
    """
    st = str(row.get('status') or '').strip().upper()
    if st and st not in STOP_ACTIVE_STATUS:
        return True, ('它当前 %s（不在投）—— 本来就没在花钱，关停没有对象，'
                      '该问的是「为什么停了」而不是「要不要停」' % st)

    opt = str(row.get('optimization_event') or '')
    if opt and 'purchase' not in opt:
        short = opt.split('.')[-1] if '.' in opt else opt
        return True, ('它的成效指标是 %s，不是 purchase —— 平台在**为 %s 找人**，'
                      '购买数少是目标决定的，不是它跑坏了' % (opt, short))

    try:
        clicks = float(row.get('clicks') or 0)
    except (TypeError, ValueError):
        clicks = None
    min_clicks = float(config.get('stop_min_clicks', STOP_MIN_CLICKS) or STOP_MIN_CLICKS)
    if clicks is not None and 0 < clicks < min_clicks:
        return True, ('窗口内只有 %g 次点击（< %g）—— 按它自己的转化率，期望单数不到 1 单，'
                      '**零单是样本不足的正常现象**，不构成止损依据'
                      % (clicks, min_clicks))

    # ⚠️ 这里**曾经**还有一条「亏损绝对额 < 100 ⇒ 不止损」，已**主动否决**（2026-10-03）。
    #    它确实能多拦一条小额亏损，但副作用更糟：一条每天只花 $5、每天亏 $3 的广告
    #    会因为「亏得不够多」而**永远不会被止损线关掉** —— 小预算持续失血是真金白银在烧。
    #    「亏 50 美元就关」是噪音；但「亏 50 美元就永远不管」是更大的错。
    #    样本量那一条（stop_min_clicks）已经把「零单但样本不足」这个真实误杀挡住了，够了。
    min_loss = None
    if min_loss is not None and abs(nv.get('net') if nv.get('net') is not None else 0.0) < min_loss:
        return True, ('亏损绝对额只有 %.2f（< %.2f）—— 钱不够本，停掉也没有可回收的优化空间，'
                      '**这不叫止损，叫噪音**' % (abs(float(nv.get('net') or 0.0)), min_loss))

    sec = row.get('video_avg_watch_sec')
    sec_low = float(config.get('stop_watch_sec_low', STOP_WATCH_SEC_LOW) or STOP_WATCH_SEC_LOW)
    if sec is not None:
        try:
            sec = float(sec)
        except (TypeError, ValueError):
            sec = None
    if sec is not None and 0 < sec < sec_low:
        return True, ('平均观看只有 %.1f 秒（< %.1f 秒）⇒ 素材只钩住了开头、后半段留不住人，'
                      '**这是素材问题不是流量问题**，该换素材；直接关停等于把「已经买到的流量」连同'
                      '优化空间一起扔掉' % (sec, sec_low))

    rc = row.get('repeat_click_ratio')
    rc_high = float(config.get('stop_repeat_click_high', STOP_REPEAT_CLICK_HIGH)
                    or STOP_REPEAT_CLICK_HIGH)
    if rc is not None:
        try:
            rc = float(rc)
        except (TypeError, ValueError):
            rc = None
    if rc is not None and rc > rc_high:
        return True, ('非独立点击占比 %.0f%%（> %.0f%%）⇒ **少数人在反复点**，'
                      '是落地页/价格/信任没接住，不是「没人感兴趣」；'
                      '减预算和关停都治不了它，该查的是转化那一环'
                      % (rc * 100.0, rc_high * 100.0))

    return False, ''


def enforce_risk_guardrails(suggestions, snapshots, config):
    """
    关停护栏（硬）+ 加预算提名（软）。

    ⚠️ 必须在「覆盖率兜底：输入 N 条 → 输出 N 条」**之后**调用。
       否则模型漏答 / 整批失败的那几条会被兜底补成 observe，直接绕过护栏 ——
       这正是它没有并进 apply_guardrails 的原因（那个函数在兜底之前跑）。

    做两件事：

    1) 关停护栏 —— 直接改 action
       · 止损线：net_view.net ≤ KILL_STOP_NET → 强制 pause
         （AI 若是 observe / increase_budget，在这里被换掉）
       · 禁杀线：net_view.net >  KILL_STOP_NET → 禁止 pause / decrease_budget，降级为 observe
       两条线互补，正好盖住两类错：该停没停（漏杀）、不该停却停了（误杀）。

    2) 加预算提名 —— **默认不改 action**，只挂 nomination 字段 + 在 reason 里加一句
       条件：net > 0（在赚）+ 顶格率 ≥ 阈值（预算卡住了它）+ 末段频次 < 阈值（受众没看腻）
             + 窗口天数 ≥ 下限（少样本的高 ROAS 是噪声，不是投放能力）
       设 config['nominate_apply'] = True 可让规则直接代替 AI 动作（**不要默认开** ——
       关于加钱这件事，默认姿态是「规则提名、人拍板」）。

    没有 net_view 的条目（例如没拿到逐日明细）保持不变，护栏不出手。

    返回 (suggestions, stats)。
    """
    stats = {'forced_pause': 0, 'blocked_kill': 0, 'nominated': 0, 'no_net': 0,
             'blocked_by_precondition': 0}
    if not suggestions:
        return suggestions, stats

    idx = _row_index(snapshots)
    stop_net = float(config.get('kill_stop_net', KILL_STOP_NET))
    fill_min = float(config.get('nominate_fill_rate', NOMINATE_FILL_RATE))
    # 🔴 提名线②：默认用**饱和线**（有行业共识），不再用 v5 拟合出来的 1.35。
    #    config 里若显式写了 nominate_freq_max 仍以它为准（保留旧配置的兼容）。
    if 'nominate_freq_max' in (config or {}):
        freq_max = float(config.get('nominate_freq_max') or NOMINATE_FREQ_SATURATED)
    else:
        freq_max = float(config.get('nominate_freq_saturated', NOMINATE_FREQ_SATURATED))
    growth_max = float(config.get('nominate_freq_growth_max', NOMINATE_FREQ_GROWTH_MAX))
    min_days = int(config.get('nominate_min_days', NOMINATE_MIN_DAYS))
    apply_nom = bool(config.get('nominate_apply', False))
    nom_pct = int(config.get('max_budget_change_pct', 20) or 20)

    for s in suggestions:
        row = idx.get(str(s.get('campaign_id'))) or {}
        nv = row.get('net_view') or {}
        net = nv.get('net')
        act = s.get('action')
        if net is None:
            stats['no_net'] += 1
            continue
        net = float(net)

        # ---------- 1) 关停护栏 ----------
        if net <= stop_net:
            if act not in STOP_ACTIONS:
                blocked, why = _stop_precondition(row, nv, config or {})
                if blocked:
                    # 护栏不越权：把判断交回 AI，但强制落到「不花钱、不关停」的 observe，
                    # 并把命中的前置条件写进 reason（人要能复核它为什么没被止损）。
                    s['ai_action'] = act
                    s['action'] = 'observe'
                    s['budget_change_pct'] = 0
                    s['guardrail'] = 'blocked_by_precondition'
                    s['stop_precondition'] = why
                    s['reason'] = ('【护栏·前置】净额 %+.2f 虽 ≤ 止损线 %.2f，但%s ⇒ '
                                   '**护栏不替你做这个决定**。请按信号本身给动作'
                                   '（换素材 / 查落地页 / 继续观察），并说明你判断的依据。原判「%s」。'
                                   '原理由：%s'
                                   % (net, stop_net, why, ACTION_CN.get(act, act),
                                      s.get('reason', '')))
                    stats['blocked_by_precondition'] += 1
                    continue
                _sp, _rv = nv.get('spend'), nv.get('revenue')
                _money = ('（花费 %.2f / 回收 %.2f）' % (float(_sp), float(_rv))
                          if _sp is not None and _rv is not None else '')
                s['ai_action'] = act
                s['action'] = 'pause'
                s['budget_change_pct'] = 0
                s['guardrail'] = 'forced_pause'
                s['reason'] = ('【护栏·止损】可见窗口净额 %+.2f%s ≤ %.2f：'
                               '这段没赚钱，放量救不了亏损 → 强制暂停（AI 原判「%s」）。原理由：%s'
                               % (net, _money, stop_net, ACTION_CN.get(act, act), s.get('reason', '')))
                stats['forced_pause'] += 1
            continue      # 已经在停，不需要再谈加钱

        if act in STOP_ACTIONS:
            s['ai_action'] = act
            s['action'] = 'observe'
            s['budget_change_pct'] = 0
            s['guardrail'] = 'blocked_kill'
            s['reason'] = ('【护栏·禁杀】可见窗口净额 %+.2f > %.2f，这段在赚钱 → 禁止关停，'
                           '降级为观察待人工复核（AI 原判「%s」）。原理由：%s'
                           % (net, stop_net, ACTION_CN.get(act, act), s.get('reason', '')))
            stats['blocked_kill'] += 1

        # ---------- 2) 加预算提名（默认只提名，不改动作）----------
        if not ENABLE_BUDGET_NOMINATION or act == 'increase_budget':
            continue
        if act == 'pause':
            continue      # 刚被止损的（或者 AI 自己判停的）不提名加钱
        # 🔴 必须先确认它**在投**。2026-10-03 重放当场抓到一个：T-07 是 PAUSED 的，
        #    AI 判的是 observe（正确），但它净额为正 + 顶格 + 频次不饱和 ⇒ 提名了。
        #    「给一条已经停投的广告建议加预算」是荒谬的。
        st = str(row.get('status') or '').strip().upper()
        if st and st not in STOP_ACTIVE_STATUS:
            continue
        fr = nv.get('fill_rate')
        fl = nv.get('freq_last')
        ff = nv.get('freq_first')
        if fr is None or fl is None:
            continue
        fr, fl = float(fr), float(fl)
        # 频次增幅：只在首末都拿到时才算，拿不到就不 disqualify（信息缺失 ≠ 不合格）
        growth = None
        if ff is not None and fl is not None:
            try:
                growth = fl - float(ff)
            except (TypeError, ValueError):
                growth = None
        growth_ok = (growth is None) or (growth <= growth_max)
        if not (fr >= fill_min and fl < freq_max and int(nv.get('days') or 0) >= min_days
                and growth_ok):
            continue
        s['nomination'] = {
            'suggested_action': 'increase_budget',
            'rule': 'net>%.2f 且 顶格率>=%.2f 且 末段频次<%.2f（饱和线）且 频次增幅<=%.2f 且 天数>=%d'
                    % (stop_net, fill_min, freq_max, growth_max, min_days),
            'net': round(net, 2), 'fill_rate': fr, 'freq_last': fl,
            'freq_growth': (round(growth, 2) if growth is not None else None),
            'applied': apply_nom,
        }
        tail = ('【提名·加预算】净额 %+.2f 在赚、顶格率 %.2f（预算卡住了它）、末段频次 %.2f'
                '（受众还没到饱和线 %.2f%s）→ 建议人工复核是否加预算'
                % (net, fr, fl, freq_max,
                   '' if growth is None else '、频次增幅 %+.2f' % growth))
        if apply_nom:
            s['ai_action'] = act
            s['action'] = 'increase_budget'
            s['budget_change_pct'] = nom_pct
            s['guardrail'] = 'rule_increase'
            tail += '（已按规则直接加 %d%%）' % nom_pct
        else:
            tail += '（本条未自动改动作）'
        s['reason'] = ('%s｜%s' % (s.get('reason', ''), tail))
        stats['nominated'] += 1

    return suggestions, stats


# ---------- 学习型判断层接入（2026-10-02）----------
# 目标：让「从历史结局里学」的表格模型（table_judger.TableJudger）能
#   ① 与 LLM 并行出建议，② 或直接替换 LLM 的动作。
# 默认 JUDGER_POLICY = 'llm' —— **不接入、行为与本改动之前完全一致**。
#
# 分工（不要绕）：
#   表格模型只产出动作；理由/矛盾仍由 LLM 写；加预算两路都只能提名；
#   无论哪条路，最终都要过 enforce_risk_guardrails（护栏压在所有模型之上）。
JUDGER_POLICY = 'llm'          # 'llm'（默认，不接入）| 'model'（模型替换）| 'blend'（有把握才用模型）


def make_table_judger(config=None, store_path=None):
    """按配置构造并加载表格模型。**没装 sklearn 时返回 None**（生产不因此崩）。

    返回的模型可能处于「未上岗」状态（标签不足）—— 那正是设计，它会自己回退规则。
    """
    try:
        from table_judger import TableJudger
    except ImportError:
        return None
    cfg = config or {}
    j = TableJudger(min_labels=int(cfg.get('judger_min_labels', 80) or 80),
                    min_confidence=float(cfg.get('judger_min_confidence', 0.55) or 0.55),
                    config=cfg, store_path=store_path)
    try:
        j.load_from_store()
    except RuntimeError:
        # 标签够了但环境没 sklearn：保持未上岗，继续回退规则，不抛给生产
        pass
    return j


def blend_with_judger(suggestions, snapshots, judger, policy=None, config=None):
    """按 policy 把表格模型的判断合并进 LLM 建议。返回 (suggestions, stats)。

      policy='llm'   → 原样返回（默认；不碰 LLM 的任何结论）
      policy='model' → 用模型的动**替换** LLM 的动作（LLM 原判写进 reason 留痕）
      policy='blend' → 仅在模型「已上岗且本条非弃权」时替换，否则保留 LLM

    ⚠️ 本函数**不调护栏**：它只做合并。合并后的结果必须再走一次
       enforce_risk_guardrails（护栏要在「合并之后」跑，否则会被合并覆盖回去）。
    """
    cfg = dict(config or {})
    pol = str(policy or cfg.get('judger_policy') or JUDGER_POLICY).lower()
    stats = {'policy': pol, 'replaced': 0, 'model_ready': False, 'model_sugg': 0}
    if not suggestions or judger is None or pol == 'llm':
        return suggestions, stats

    stats['model_ready'] = bool(getattr(judger, 'is_ready', lambda: False)())
    model_sugg = judger.judge(snapshots, cfg, apply_guardrail=False)
    stats['model_sugg'] = len(model_sugg)
    by_id = {str(s.get('campaign_id')): s for s in model_sugg}

    for s in suggestions:
        ms = by_id.get(str(s.get('campaign_id')))
        if not ms:
            continue
        usable = (pol == 'model') or (pol == 'blend' and stats['model_ready']
                                      and ms.get('source') == 'table_model')
        if not usable:
            continue
        old = s.get('action')
        if old == ms['action']:
            continue
        s['llm_action'] = old
        s['action'] = ms['action']
        s['budget_change_pct'] = 0 if ms['action'] != 'increase_budget' else ms.get('budget_change_pct', 0)
        s['judger_source'] = ms.get('source')
        s['reason'] = ('【表格模型改判｜%s】%s（LLM 原判「%s」）'
                       % (ms.get('source'), ms.get('reason', ''),
                          ACTION_CN.get(old, old)))
        if ms.get('nomination') and ms['action'] == 'increase_budget':
            s['nomination'] = ms['nomination']
        stats['replaced'] += 1
    return suggestions, stats
