# -*- coding: utf-8 -*-
"""
把「上传的广告数据文件」读成 AI 判断pipeline 认识的 insights 结构。

为什么单独一个模块：
  生产路径的 insights 来自 `api_fb.FacebookAdsApp._ai_get_insights`（打 Facebook Graph API）。
  测试路径要让 AI 判同一份数据、但数据来自本地文件 —— 两边必须产出**同一套字段**，
  否则「文件测出来好不好」跟「真实账户判出来好不好」根本不能比。
  所以这里只做一件事：文件 -> 与 _ai_get_insights 返回结构完全一致的 list[dict]。

支持的输入：
  ① Facebook Ads Manager「逐日导出」CSV / XLSX（表头中文，含 报告开始日期 / 广告系列名称 /
     已花费金额 (USD) / 加入购物车次数 / 购物次数 ... ）。这是最贴近真实的一档 ——
     可以直接从 Ads Manager 导出，也可以拿来测模拟数据。
  ② 若文件里带 `广告名称` / `广告 ID` 列，就按**广告**粒度聚合（更细，推荐）；
     只有 `广告系列名称` 时按系列粒度聚合。

设计原则（踩过的坑）：
  · **不造字段**。文件里没有的列（如「内容查看次数」）一律按 0 走并在 meta.data_gaps 里明说，
    绝不按 `加购 × 4` 之类反推 —— 那会让 AI 拿到一份看似完整、实则虚构的漏斗，
    「测出来的 AI 水平」也就没有意义了。
  · **零花费的天要补齐**（和生产的补零逻辑一致）：Ads Manager 不导出「一分钱没投」的天，
    「今天没花钱」在文件里是**缺行**而不是 0。若不补，AI 会把「停投」读成「数据没更新」，
    永远答不了「从哪天起停的」。补零范围 = [该广告自己的首行日期, 文件最后一天]。
"""
import csv
import os
from collections import OrderedDict
from datetime import datetime, timedelta

# ---------- 列名映射（尽量吃下 Ads Manager 各种导出变体） ----------
COL_DATE = ['报告开始日期', '日期', 'date', 'day']
COL_AD = ['广告名称', '广告名', 'ad_name', 'ad name']
COL_AD_ID = ['广告 ID', '广告id', 'ad_id', 'ad id']
COL_CAMP = ['广告系列名称', '系列名称', 'campaign_name']
COL_STATUS = ['广告状态', '广告投放', '广告系列投放', '投放状态', 'status']
COL_OBJ = ['广告系列目标', '广告目标', 'objective']
COL_METRIC = ['成效指标', 'results_indicator', '优化事件']
# ⚠️ 别把「成效」列放进别名表：那是**数字列**（这一行有几次成效），
#    而「成效指标」是**文本列**（优化的是购买还是加购）。混在一起会让
#    metric_sample 变成 '1' 这种数字，_infer_objective 就推不出目标了。
COL_SPEND = ['已花费金额 (USD)', '已花费金额', '花费金额', '金额 (USD)', 'spend']
COL_IMP = ['展示次数', '展示量', 'impressions']
COL_REACH = ['覆盖人数', '触达人数', 'reach']
COL_CLICK = ['链接点击量', '链接点击次数', 'inline_link_clicks', 'clicks']
COL_CPC = ['单次链接点击费用 (USD)', '单次链接点击费用', 'cpc']
COL_CPM = ['CPM（千次展示费用） (USD)', 'CPM (USD)', 'CPM（千次展示费用）', 'cpm']
COL_CART = ['加入购物车次数', '加购次数', 'add_to_cart']
COL_CHECKOUT = ['结账发起次数', '发起结账次数', 'initiate_checkout']
COL_PURCH = ['购物次数', '购买次数', 'purchase']
COL_PAY = ['添加支付信息', '添加支付信息次数', 'add_payment_info']
COL_PVAL = ['购物转化价值', '购买转化价值', 'purchase_value']
COL_VIEW = ['内容查看次数', '落地页浏览量', '内容查看', 'view_content', 'landing_page_view']
COL_ADSET_BUDGET = ['广告组预算', 'adset_budget']
COL_ADSET_BUDGET_TYPE = ['广告组预算类型', 'adset_budget_type']
COL_CAMP_BUDGET = ['广告系列预算', '系列预算', 'campaign_budget']
# ---- P0/P1 新增：口径与竞争对照列（2026-10-02）----
# 这三列的意义不是「再多一个数字」，而是**决定已有数字该怎么读**：
#   · 归因设置  → 同一份 spend/ROAS 在不同窗口下不可比（7d_click vs 1d_view 差很多）
#   · 成效指标  → 这条广告组优化的到底是「购买」还是「加购」；优化加购的广告天生买得少，
#                 拿「购买少」去关它是误杀
#   · 质量/互动率/转化率排名 → 唯一带「竞争对照」的信号（和抢同批受众的广告比）。
#                 注意：Meta 只在有足够投放时给排名，没跑起来的广告这一列是 '-'（空）。
COL_ATTR = ['归因设置', 'attribution_setting', 'attribution']
COL_METRIC_RAW = ['成效指标', 'results_indicator', '优化事件', 'optimization_goal']
COL_RANK_QUALITY = ['质量排名', 'quality_ranking']
COL_RANK_ENGAGE = ['互动率排名', 'engagement_rate_ranking']
COL_RANK_CONV = ['转化率排名', 'conversion_rate_ranking']
COL_END_DATE = ['结束日期', 'end_date', 'end_time']
COL_COST_THRUPLAY = ['单次 ThruPlay 费用 (USD)', '单次 ThruPlay 费用', 'cost_per_thruplay']

# 空值哨兵：Ads Manager 用 '-' / '--' / '' 表示「这项没有数据」，
# 与「真实的 0」是两回事，绝不能混为一谈（排名的 '-' ≠ 排名最差）。
_EMPTY_TOKENS = ('', '-', '--', '—', 'n/a', 'na', 'null', 'none', '不适用')


class LoadError(Exception):
    """文件读不了 / 不像广告数据 —— 消息直接给用户看，所以写成人话。"""


def _norm(s):
    """列名归一：去空格/全角空格、去 BOM、lower。"""
    return str(s or '').replace('﻿', '').replace(' ', '').replace('\u3000', '').strip().lower()


class _Row(dict):
    """按归一化列名取值的小包装（原列名保持原样返回）。"""
    def __init__(self, raw):
        super().__init__(raw)
        self._idx = {_norm(k): k for k in raw.keys()}

    def pick(self, names, default=None):
        for n in names:
            k = self._idx.get(_norm(n))
            if k is not None and raw_val(self, k) not in (None, ''):
                return raw_val(self, k)
        return default

    def has(self, names):
        return any(_norm(n) in self._idx for n in names)


def raw_val(row, key):
    v = row.get(key)
    if v is None:
        return None
    return v.strip() if isinstance(v, str) else v


def _num(v, cast=float, default=0):
    if v in (None, ''):
        return default
    try:
        if isinstance(v, str):
            v = v.replace(',', '').replace('$', '').replace('￥', '').strip()
            if v == '' or v == '-':
                return default
        return cast(float(v))
    except (TypeError, ValueError):
        return default


def _read_csv(path):
    # utf-8-sig 吃掉 Ads Manager 导出带的 BOM，否则第一个列名会多一个 \ufeff
    with open(path, encoding='utf-8-sig', newline='') as f:
        return [dict(r) for r in csv.DictReader(f)]


def _read_xlsx(path):
    try:
        import openpyxl
    except ImportError:
        raise LoadError("读 .xlsx 需要 openpyxl（本机 Python312 已装）；"
                        "也可直接把表格另存为 CSV 再上传。")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    it = ws.iter_rows(values_only=True)
    try:
        header = [str(c) if c is not None else '' for c in next(it)]
    except StopIteration:
        raise LoadError("这个表是空的。")
    rows = []
    for r in it:
        if all(c is None or str(c).strip() == '' for c in r):
            continue
        rows.append({header[i]: ('' if v is None else v) for i, v in enumerate(r) if i < len(header)})
    wb.close()
    return rows


def _text_or_none(v):
    """取文本值；Ads Manager 的空值哨兵（'-' / '' / 'n/a' …）一律返回 None。

    为什么必须区分：排名列的 '-' 表示「Meta 没有给出排名」（通常是投放量不够），
    不是「排名等于某个值」。若当成普通文本传下去，AI 会把 '-' 当成一种排名档位来解读。
    """
    if v is None:
        return None
    s = str(v).strip()
    if s.lower() in _EMPTY_TOKENS:
        return None
    return s


def _infer_objective(metric):
    """从『成效指标』反推目标（形如 actions:offsite_conversion.fb_pixel_purchase）。"""
    m = _norm(metric)
    if 'purchase' in m or 'offsite_conversion' in m:
        return 'OUTCOME_SALES'
    if 'engagement' in m or 'post_engagement' in m:
        return 'OUTCOME_ENGAGEMENT'
    if 'link_click' in m or 'landing_page_view' in m:
        return 'OUTCOME_TRAFFIC'
    return ''


def _is_cbo(type_text, camp_budget):
    t = _norm(type_text)
    if '系列预算' in str(type_text) or 'campaignbudget' in t or 'campaign_budget' in t:
        return True
    if _num(camp_budget, float, 0) > 0 and '广告组预算' not in str(type_text):
        return True
    return False


def load_file(path, fill_zero_days=True, max_rows=200000):
    """读文件 -> (rows, meta)。

    rows: list[dict]，字段与 `api_fb._ai_get_insights` 的返回**完全一致**
          （id/name/status/objective/budget_type/ad_type/spend/impressions/reach/frequency/
            clicks/cpc/cpm/purchase/purchase_value/cost_per_purchase/add_to_cart/
            initiate_checkout/add_payment_info/view_content/campaign_daily_budget/
            adset_daily_budget/start_time/daily_spend[...]）
    meta: {file, n_ads, n_ad_days, n_ad_days_filled, window, grain, statuses, data_gaps, ...}
    """
    if not path or not os.path.exists(path):
        raise LoadError("文件不存在：%s" % path)
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.xlsx', '.xlsm'):
        raw = _read_xlsx(path)
    elif ext == '.xls':
        raise LoadError("旧版 .xls 需要另存为 .xlsx 或 CSV 再上传（本机没装 xlrd）。")
    elif ext in ('.csv', '.txt'):
        raw = _read_csv(path)
    else:
        raise LoadError("不认的文件类型 %s，请给 .csv 或 .xlsx。" % ext)

    if not raw:
        raise LoadError("文件里没有数据行（只有表头？）。")

    rows_in = [_Row(r) for r in raw[:max_rows]]
    probe = rows_in[0]

    # ---- 结构校验：必须先确认这是「逐日广告数据」 ----
    if not probe.has(COL_DATE):
        raise LoadError("找不到日期列（需要『报告开始日期』或『日期』）。\n"
                        "当前表头：%s\n"
                        "→ 请用 Ads Manager 的『逐日』导出，或带 date 列的 CSV。"
                        % '、'.join(list(probe.keys())[:12]))
    if not (probe.has(COL_SPEND) or probe.has(COL_IMP)):
        raise LoadError("找不到花费/展示列（需要『已花费金额 (USD)』或『展示次数』）。\n"
                        "当前表头：%s" % '、'.join(list(probe.keys())[:12]))
    if not (probe.has(COL_AD) or probe.has(COL_CAMP) or probe.has(COL_AD_ID)):
        raise LoadError("找不到广告/系列标识列（需要『广告名称』或『广告系列名称』）。")

    # ---- 粒度：有广告列就按广告聚合（更细），否则退到系列 ----
    grain = 'ad'
    if probe.has(COL_AD):
        key_fn = lambda r: (r.pick(COL_AD_ID) or '', r.pick(COL_AD) or '', r.pick(COL_CAMP) or '')
    elif probe.has(COL_AD_ID):
        key_fn = lambda r: (r.pick(COL_AD_ID) or '', '', r.pick(COL_CAMP) or '')
    else:
        grain = 'campaign'
        key_fn = lambda r: ('', '', r.pick(COL_CAMP) or '')

    # ---- 聚合 ----
    groups = OrderedDict()
    all_dates = []
    skipped = 0
    for r in rows_in:
        d = r.pick(COL_DATE)
        d10 = str(d)[:10]
        try:
            datetime.strptime(d10, '%Y-%m-%d')
        except (TypeError, ValueError):
            skipped += 1
            continue
        all_dates.append(d10)
        groups.setdefault(key_fn(r), []).append(r)

    if not groups:
        raise LoadError("没有任何一行带可识别的日期（YYYY-MM-DD）。")

    window_start, window_end = min(all_dates), max(all_dates)

    out = []
    n_filled = 0
    gaps = set()
    if not probe.has(COL_VIEW):
        gaps.add('内容查看（view_content）：本文件无此列，一律按 0 —— '
                 'AI 看到的漏斗从「加购」起算，别把它当成真实的零内容查看。')
    if not probe.has(COL_REACH):
        gaps.add('覆盖人数（reach）：本文件无此列 → frequency 无法计算，按 0 走。')
    if grain == 'campaign':
        gaps.add('本文件只有系列列、没有广告列 → 按**系列**粒度聚合（AI 会把整个系列当一个投放对象看）。')

    for (ad_id, ad_name, camp_name), days in groups.items():
        days_sorted = sorted(days, key=lambda r: str(r.pick(COL_DATE))[:10])

        tot = dict(spend=0.0, imp=0, reach=0, clicks=0, purch=0,
                   pval=0.0, cart=0, co=0, pay=0, view=0)
        daily_map = {}
        objective = ''
        status = ''
        adset_budget = None
        camp_budget = None
        adset_budget_type = ''
        metric_sample = ''
        # P0/P1：口径与竞争对照（整条广告取首个非空值 —— 这些列逐日重复）
        attribution = None
        rank_quality = None
        rank_engage = None
        rank_conv = None
        end_date = None
        cost_thruplay = None
        first_date = str(days_sorted[0].pick(COL_DATE))[:10]

        for d in days_sorted:
            date10 = str(d.pick(COL_DATE))[:10]
            spend = _num(d.pick(COL_SPEND), float)
            imp = _num(d.pick(COL_IMP), int)
            reach = _num(d.pick(COL_REACH), int)
            clicks = _num(d.pick(COL_CLICK), int)
            purch = _num(d.pick(COL_PURCH), int)
            pval = _num(d.pick(COL_PVAL), float)
            cart = _num(d.pick(COL_CART), int)
            co = _num(d.pick(COL_CHECKOUT), int)
            pay = _num(d.pick(COL_PAY), int)
            view = _num(d.pick(COL_VIEW), int)

            if not metric_sample and d.pick(COL_METRIC):
                metric_sample = str(d.pick(COL_METRIC))
            if not objective and d.pick(COL_OBJ):
                objective = str(d.pick(COL_OBJ)).strip()
            if not status and d.pick(COL_STATUS):
                status = str(d.pick(COL_STATUS)).strip()
            if adset_budget is None and d.pick(COL_ADSET_BUDGET) not in (None, ''):
                adset_budget = _num(d.pick(COL_ADSET_BUDGET), float, None)
            if camp_budget is None and d.pick(COL_CAMP_BUDGET) not in (None, ''):
                camp_budget = _num(d.pick(COL_CAMP_BUDGET), float, None)
            if not adset_budget_type and d.pick(COL_ADSET_BUDGET_TYPE):
                adset_budget_type = str(d.pick(COL_ADSET_BUDGET_TYPE)).strip()
            # P0/P1：空值哨兵 '-' 一律读成 None（= 没有这项数据），不当成取值
            if attribution is None:
                attribution = _text_or_none(d.pick(COL_ATTR))
            if rank_quality is None:
                rank_quality = _text_or_none(d.pick(COL_RANK_QUALITY))
            if rank_engage is None:
                rank_engage = _text_or_none(d.pick(COL_RANK_ENGAGE))
            if rank_conv is None:
                rank_conv = _text_or_none(d.pick(COL_RANK_CONV))
            if end_date is None:
                end_date = _text_or_none(d.pick(COL_END_DATE))
            if cost_thruplay is None and d.pick(COL_COST_THRUPLAY) not in (None, ''):
                cost_thruplay = _num(d.pick(COL_COST_THRUPLAY), float, None)

            tot['spend'] += spend
            tot['imp'] += imp
            tot['reach'] += reach
            tot['clicks'] += clicks
            tot['purch'] += purch
            tot['pval'] += pval
            tot['cart'] += cart
            tot['co'] += co
            tot['pay'] += pay
            tot['view'] += view

            daily_map[date10] = {
                'date': date10, 'spend': round(spend, 2), 'impressions': imp,
                'clicks': clicks, 'purchase': purch, 'purchase_value': round(pval, 2),
                'add_to_cart': cart, 'initiate_checkout': co, 'add_payment_info': pay,
                'view_content': view, 'reach': reach,
                'frequency': round(imp / reach, 2) if reach else 0.0,
            }

        # 补零：Ads Manager 不导出「零投放的天」，缺行要补成 spend:0（否则 AI 读成「数据没更新」）
        if fill_zero_days:
            d0 = datetime.strptime(first_date, '%Y-%m-%d')
            d1 = datetime.strptime(window_end, '%Y-%m-%d')
            cur = d0
            while cur <= d1:
                ds = cur.strftime('%Y-%m-%d')
                if ds not in daily_map:
                    daily_map[ds] = {'date': ds, 'spend': 0.0, 'impressions': 0, 'clicks': 0,
                                     'purchase': 0, 'purchase_value': 0.0, 'add_to_cart': 0,
                                     'initiate_checkout': 0, 'add_payment_info': 0,
                                     'view_content': 0, 'reach': 0, 'frequency': 0.0}
                    n_filled += 1
                cur += timedelta(days=1)

        daily = [daily_map[k] for k in sorted(daily_map.keys())]
        name = ad_name or camp_name or ad_id or '(无名)'
        ident = ad_id or ad_name or camp_name
        cbo = _is_cbo(adset_budget_type, camp_budget)

        # 预算落到正确的层级 —— 层级错了 AI 会给错动作：
        #   ABO：预算在广告组（adset_daily_budget），调预算直接影响这一条
        #   CBO：预算在系列（campaign_daily_budget），系列内广告共享，动它影响整组
        # 实测坑（700 行文件）：CBO 对象的 `广告组预算类型` 写的是「使用广告系列预算」，
        # 但**导出里没有单独的『广告系列预算』列**，钱只出现在『广告组预算』列里。
        # 若照字面当 ABO 处理，或干脆丢掉，AI 就会看到「CBO 但预算未知」——
        # 而 prompt 明确要求 CBO 的定级建议要看系列预算。所以这里把它归到系列层。
        if cbo:
            camp_final = camp_budget if camp_budget is not None else adset_budget
            adset_final = None
        else:
            camp_final = camp_budget
            adset_final = adset_budget

        # 目标：优先取文件里的「广告系列目标」列，没有再从成效指标反推
        obj = objective.upper().strip() if objective else ''
        if not obj:
            obj = _infer_objective(metric_sample) or 'OUTCOME_SALES'
        if obj and not obj.startswith('OUTCOME_') and obj.upper() in ('SALES', 'ENGAGEMENT', 'TRAFFIC', 'AWARENESS'):
            obj = 'OUTCOME_' + obj.upper()

        rec = {
            'id': ident,
            'name': name,
            'status': (status or 'ACTIVE').upper(),
            'objective': obj,
            'budget_type': 'CBO' if cbo else 'ABO',
            'campaign_daily_budget': camp_final,
            'adset_daily_budget': adset_final,
            'ad_type': '基础',
            'spend': round(tot['spend'], 2),
            'impressions': tot['imp'],
            'reach': tot['reach'],
            'frequency': round(tot['imp'] / tot['reach'], 2) if tot['reach'] else 0.0,
            'clicks': tot['clicks'],
            'cpc': round(tot['spend'] / tot['clicks'], 2) if tot['clicks'] else 0,
            'cpm': round(tot['spend'] * 1000 / tot['imp'], 2) if tot['imp'] else 0,
            'purchase': tot['purch'],
            'purchase_value': round(tot['pval'], 2),
            'cost_per_purchase': round(tot['spend'] / tot['purch'], 2) if tot['purch'] else 0,
            'add_to_cart': tot['cart'],
            'initiate_checkout': tot['co'],
            'add_payment_info': tot['pay'],
            'view_content': tot['view'],
            'start_time': first_date + 'T00:00:00+0800',
            'daily_spend': daily,
        }
        # ---- P0/P1：只带非空项（Graph API 缺字段时也是不带）----
        # 这些字段的意义是「决定别的数字怎么读」，不是再加一个数字：
        #   attribution        归因窗口（7d_click / 1d_view …）—— 跨窗口的 ROAS 不可比
        #   optimization_event 这个广告组在优化什么 —— 优化加购的广告不因「购买少」被关
        #   rankings           质量 / 互动率 / 转化率排名 —— 唯一的竞争对照；缺 = 投放量不够
        if attribution:
            rec['attribution'] = attribution
        if metric_sample:
            rec['optimization_event'] = metric_sample
        if rank_quality or rank_engage or rank_conv:
            rec['rankings'] = {k: v for k, v in (
                ('quality', rank_quality), ('engagement', rank_engage),
                ('conversion', rank_conv)) if v}
        if end_date:
            rec['end_date'] = end_date
        if cost_thruplay is not None:
            rec['cost_per_thruplay'] = cost_thruplay
        out.append(rec)

    statuses = {}
    for r in out:
        statuses[r['status']] = statuses.get(r['status'], 0) + 1

    # 聚合完才能算得出的缺口（依赖 out）
    n_cbo = sum(1 for r in out if r['budget_type'] == 'CBO')
    if n_cbo:
        gaps.add('其中 %d 条是 CBO（系列预算型）：导出里没有单独的『广告系列预算』列时，'
                 '本加载器把『广告组预算』列的值当作系列日预算用（有该列则优先用它）。' % n_cbo)
    n_nobudget = sum(1 for r in out
                     if not r.get('campaign_daily_budget') and not r.get('adset_daily_budget'))
    if n_nobudget:
        gaps.add('其中 %d 条完全没有预算数字（导出里两列都空）→ AI 无法判断「花费离预算还有多远」。'
                 % n_nobudget)

    # P0/P1 缺口声明：排名列「有列但全空」与「根本没有这列」是两回事，都必须明说——
    # 否则 AI 会把「Meta 没给排名」读成「排名很差」，或把「没这列」读成「排名为 0」。
    has_rank_col = (probe.has(COL_RANK_QUALITY) or probe.has(COL_RANK_ENGAGE)
                    or probe.has(COL_RANK_CONV))
    n_no_rank = sum(1 for r in out if not r.get('rankings'))
    if has_rank_col and n_no_rank:
        gaps.add('质量/互动率/转化率排名：导出里有这几列，但 %d/%d 条是空的（Ads Manager 显示 "-"）。'
                 'Meta 只在投放量足够时才给排名 —— 空 = 没有这项数据，**不是排名差**。'
                 % (n_no_rank, len(out)))
    elif not has_rank_col:
        gaps.add('质量/互动率/转化率排名：本文件没有这三列 → 没有「和同场竞品比」的对照信息，'
                 '判断只能基于自身数字，不得断言「这条比同行好/差」。')
    if not probe.has(COL_ATTR):
        gaps.add('归因设置：本文件没有这一列 → 归因窗口未知，'
                 '跨来源的 ROAS / 购买数不可直接比大小。')

    meta = {
        'file': os.path.basename(path),
        'path': os.path.abspath(path),
        'grain': grain,
        'n_rows_in': len(rows_in),
        'n_skipped': skipped,
        'n_ads': len(out),
        'n_ad_days': sum(len(r['daily_spend']) for r in out),
        'n_ad_days_filled': n_filled,
        'window': (window_start, window_end),
        'days': (datetime.strptime(window_end, '%Y-%m-%d')
                 - datetime.strptime(window_start, '%Y-%m-%d')).days + 1,
        'total_spend': round(sum(r['spend'] for r in out), 2),
        'total_purchase': sum(r['purchase'] for r in out),
        'statuses': statuses,
        'data_gaps': sorted(gaps),
    }
    return out, meta


if __name__ == '__main__':
    import sys
    argv = sys.argv[1:] or [os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         'data', '模拟广告数据-100广告7天.csv')]
    rows, meta = load_file(argv[0])
    print('文件：%s' % meta['file'])
    print('粒度：%s | 广告数 %d | 广告-日 %d（其中补零 %d）| 输入行 %d（跳过 %d）'
          % (meta['grain'], meta['n_ads'], meta['n_ad_days'], meta['n_ad_days_filled'],
             meta['n_rows_in'], meta['n_skipped']))
    print('时间窗：%s ~ %s（%d 天）| 合计花费 $%.2f | 合计购买 %d'
          % (meta['window'][0], meta['window'][1], meta['days'],
             meta['total_spend'], meta['total_purchase']))
    print('状态：%s' % meta['statuses'])
    for g in meta['data_gaps']:
        print('数据缺口：%s' % g)
