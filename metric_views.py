# -*- coding: utf-8 -*-
"""metric_views.py —— 多视图接入层：同一批广告的**多份不同列的导出** → 一张宽表。

为什么要它
==========
投手看一个账户时，永远是「一次挑一套列」：要么看转化漏斗，要么看点击质量，
要么看视频留存。四套列之间的**关系**（比如「点击很多但落地页到达率很低」、
「3 秒播放不错但 25% 就掉一半」）从来不在同一张表里，所以人工几乎不会去横着看。

机器不受这个限制：把同一账户、同一天的几份导出按 (日期, 广告) 拼起来，
就能一次看到全部维度。这份文件就是干这个的。

实测依据（2026-10-02，真实账户 6 份导出）
========================================
6 份文件里只有 **5 种不同的指标视图**，其中 1 份是同表头的子集（应丢弃）：

    表头指纹（归一化列名集合）相同 = 同一套指标，行数少的那份直接扔

并且**重叠行逐格一致**（1251/1251、890/890、885/885），证明它们是同一账户事实的不同列
预设，而不是不同口径的报表 —— 所以拼接是安全的。遇到不一致时会记进 `conflicts`，
**绝不静默取一个**（实测那份旧导出在 03-10 的花费是 7.56 而新导出是 21.53，差 3 倍）。

三条硬边界
==========
1. **核心 30 维一个字都不动。** 它们定义在 `table_judger._features_from_days`，
   改了历史成绩全部作废。本模块的扩展特征是**另一个块**（`EXT_FEATURE_KEYS`），
   目前**不喂给模型**，只做呈现与诊断 —— 要不要让它进模型，得先有能验证它的数据。
2. **不可算 ≠ 0。** 除数为 0 或该列压根没导出时返回 `None`，不是 0.0。
   （教训：缺列静默变 0 会让人把「没读到」当成「真的没有」。）
3. 本模块**不产判断、不碰护栏**。它只负责「把数据凑齐」。

诚实说明（别把这份能力吹大）
============================
仓库自带的模拟数据（100 广告 × 10 天）**只有转化视图那一套列**，
所以扩展特征在这套「考场」上**无法验证是否提升判断力**。
真账户目前也只有 3 条广告、真实投放只覆盖 03-07~03-10，视频/互动视图更是只有 4 天。
⇒ 现在能做的是「接入 + 看见」，不是「证明更强」。
"""
import os
import glob
import sys
from collections import OrderedDict

D = os.path.dirname(os.path.abspath(__file__))
if D not in sys.path:
    sys.path.insert(0, D)

from table_judger import (      # noqa: E402
    norm_col, read_csv_rows, resolve_col, resolve_group_col,
)


# ==================== 1. 五种视图的识别 ====================
# signature = 该视图**独有**的列（归一化后精确匹配）。命中数最多的视图胜出。
# 顺序只用于同分兜底，不参与主判。
VIEW_SPECS = OrderedDict([
    ('click', {
        'cn': '点击视图',
        'desc': '点击质量 + 落地页：链接点击率 / 全部点击率 / 落地页浏览量 / 单次浏览费用',
        'signature': ['点击量（全部）', '点击率（全部）', '单次点击费用（全部） (USD)',
                      '落地页浏览量', '落地页单次浏览费用 (USD)', 'shop_clicks', '链接点击率'],
        'ext': ['全部点击倍数', '落地页到达率', '落地页单次浏览成本'],
    }),
    ('video', {
        'cn': '视频视图',
        'desc': '素材留存：3 秒播放 / ThruPlay / 25~100% 播放进度',
        'signature': ['ThruPlay 次数', '播放视频达 3 秒的次数', '持续播放视频达 2 秒的次数',
                      '视频播放进度达 25% 的次数', '视频播放进度达 50% 的次数',
                      '视频播放进度达 75% 的次数', '视频播放进度达 95% 的次数',
                      '视频播放进度达 100% 的次数', '视频播放量'],
        'ext': ['3秒播放率', 'ThruPlay率', '视频留存25', '视频留存50', '视频完播率'],
    }),
    ('engagement', {
        'cn': '互动视图',
        'desc': '主页互动：互动量 / 心情 / 评论 / 收藏 / 分享 / FB 获赞 / IG 关注',
        'signature': ['公共主页互动量', '帖子心情', '帖子评论数', '帖子收藏',
                      '帖子分享次数', 'Facebook 获赞数', 'Instagram 关注次数'],
        'ext': ['互动率', '评论率', '分享率', '涨粉率', '互动涨粉成本'],
    }),
    ('conversion', {
        'cn': '转化视图',
        'desc': '购物漏斗：加购 → 结账 → 支付 → 购买 + 金额 + ROAS（核心 30 维就靠它）',
        'signature': ['加入购物车次数', '结账发起次数', '购物次数', '添加支付信息',
                      '购物转化价值', '广告花费回报 (ROAS) - 购物'],
        'ext': [],
    }),
    ('cost', {
        'cn': '成效费用视图',
        'desc': '成效 + 单次成效费用 + 结束日期（最少列，通常配合别的视图用）',
        'signature': ['单次成效费用', '结束日期'],
        'ext': [],
    }),
])

ALL_VIEWS = tuple(VIEW_SPECS.keys())

# 缺某个视图时，会连带失去什么（人话，用于购物清单）
VIEW_LOSS = {
    'conversion': '核心 30 维整块不可用（漏斗 5 列 + 加购率/结账率/支付率/购买率/客单价/单均成本…）',
    'click': '全部点击倍数、落地页到达率、落地页单次浏览成本',
    'video': '3秒播放率、ThruPlay率、视频留存25/50、视频完播率',
    'engagement': '互动率、评论率、分享率、涨粉率、互动涨粉成本',
    'cost': '无（这个视图只提供交叉校验用的单次成效费用）',
}


def header_sig(header):
    """表头指纹：归一化列名集合（与顺序无关）。指纹相同 = 同一套指标。"""
    return frozenset(norm_col(h) for h in header)


def detect_view(header):
    """按签名列命中数判视图。返回 (视图名, 命中的签名列数)。认不出返回 ('unknown', 0)。"""
    norm = set(norm_col(h) for h in header)
    best, best_hit = 'unknown', 0
    for name, spec in VIEW_SPECS.items():
        hit = sum(1 for c in spec['signature'] if norm_col(c) in norm)
        if hit > best_hit:
            best, best_hit = name, hit
    return (best, best_hit) if best_hit > 0 else ('unknown', 0)


def view_cn(name):
    return (VIEW_SPECS.get(name) or {}).get('cn', '未识别')


# ==================== 2. 读一份导出 ====================

class Export(object):
    """一份导出的元信息 + 原始行。"""

    __slots__ = ('path', 'name', 'header', 'rows', 'view', 'hit', 'sig',
                 'mtime', 'group_col', 'date_col')

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.rows = read_csv_rows(path)
        self.header = list(self.rows[0].keys()) if self.rows else []
        self.view, self.hit = detect_view(self.header)
        self.sig = header_sig(self.header)
        self.mtime = os.path.getmtime(path)
        self.group_col = resolve_group_col(self.header)
        self.date_col = resolve_col(self.header, ['报告开始日期', '日期', 'date', 'day'])

    @property
    def n(self):
        return len(self.rows)

    def ads(self):
        if not self.group_col:
            return []
        return sorted(set(str(r.get(self.group_col) or '') for r in self.rows))

    def keys(self):
        """(日期, 广告) 键集合。"""
        if not self.group_col or not self.date_col:
            return set()
        return set((str(r.get(self.date_col) or ''), str(r.get(self.group_col) or ''))
                   for r in self.rows)

    def __repr__(self):
        return '<Export %s view=%s n=%d>' % (self.name, self.view, self.n)


def load_export(path):
    return Export(path)


def scan_dir(pattern, folder):
    return sorted(glob.glob(os.path.join(folder, pattern)))


# ==================== 3. 过滤相同的 → 留下不同的 ====================

def dedupe_exports(exports):
    """按表头指纹分组，每组只留一份。

    留下的规则：**数据行最多**优先，同样多则取文件更新的那份。
    返回 (groups, chosen, dropped)：
        groups   = [{'sig','view','members':[Export,...],'kept':Export,'dropped':[Export,...]}]
        chosen   = [Export,...]   每个指纹的胜者（输入顺序）
        dropped  = [Export,...]   被丢弃的
    """
    buckets = OrderedDict()
    for e in exports:
        buckets.setdefault(e.sig, []).append(e)

    groups, chosen, dropped = [], [], []
    for sig, members in buckets.items():
        ranked = sorted(members, key=lambda e: (-e.n, -e.mtime))
        kept, losers = ranked[0], ranked[1:]
        groups.append({'sig': sig, 'view': kept.view, 'members': members,
                       'kept': kept, 'dropped': losers})
        chosen.append(kept)
        dropped.extend(losers)
    return groups, chosen, dropped


# ==================== 4. 合并成宽表 ====================

def merge_exports(exports, prefer='newest'):
    """把多份导出按 (日期, 广告) 合并成宽表。

    prefer='newest'：**文件更新的优先**（冲突时它的值胜出）。
    返回 (wide, report)：
        wide[key] = {原始列名: 值}      # 所有视图的列都在这
        report = {'conflicts':[{key,col,winner,loser,winner_file,loser_file}], 'n_keys':..}
    冲突**从不静默**：同一 (日期,广告) 的同一列，两份导出给了不同值 → 记下来。
    """
    if prefer == 'newest':
        order = sorted(exports, key=lambda e: -e.mtime)
    elif prefer == 'most_rows':
        order = sorted(exports, key=lambda e: (-e.n, -e.mtime))
    else:
        order = list(exports)

    wide = OrderedDict()
    owner = {}       # (key, col) → 写下这个值的 Export
    conflicts = []
    for e in order:
        if not e.group_col or not e.date_col:
            continue
        for r in e.rows:
            key = (str(r.get(e.date_col) or ''), str(r.get(e.group_col) or ''))
            slot = wide.setdefault(key, {})
            for col, val in r.items():
                v = ('' if val is None else str(val)).strip()
                if col not in slot:
                    slot[col] = v
                    owner[(key, col)] = e
                    continue
                prev = (slot[col] or '').strip()
                if prev == v:
                    continue
                # 两边都空 = 没冲突；一边空一边有 = 也不吵（取值更全的那份）
                if not prev or not v:
                    if not prev and v:
                        slot[col] = v
                        owner[(key, col)] = e
                    continue
                conflicts.append({
                    'key': key, 'col': col,
                    'winner': prev, 'loser': v,
                    'winner_file': owner[(key, col)].name, 'loser_file': e.name,
                })
    return wide, {'conflicts': conflicts, 'n_keys': len(wide)}


def wide_header(wide):
    """宽表里出现过的全部原始列名（保持首次出现的顺序）。"""
    cols = OrderedDict()
    for slot in wide.values():
        for c in slot:
            cols.setdefault(c, None)
    return list(cols.keys())


def wide_rows_by_ad(wide):
    """{广告名: [宽表行, ...]}（按日期排序）。"""
    by = OrderedDict()
    for (date, ad), slot in wide.items():
        row = dict(slot)
        row['__date'] = date
        by.setdefault(ad, []).append(row)
    for ad in by:
        by[ad].sort(key=lambda r: r.get('__date', ''))
    return by


# ==================== 5. 扩展特征（核心 30 维之外的那一块）====================
# 全部是「窗口合计」再求比值 —— 比值才有可比性，绝对数随窗口长短漂。
EXT_RAW_ALIASES = OrderedDict([
    ('impressions', ['展示次数', '展示量', '展示']),
    ('clicks', ['链接点击量', '链接点击次数', '链接点击']),
    ('spend', ['已花费金额 (USD)', '已花费金额', '花费']),
    ('click_all', ['点击量（全部）', '点击（全部）']),
    ('landing_views', ['落地页浏览量']),
    ('video_2s', ['持续播放视频达 2 秒的次数']),
    ('video_3s', ['播放视频达 3 秒的次数']),
    ('thruplay', ['ThruPlay 次数']),
    ('v25', ['视频播放进度达 25% 的次数']),
    ('v50', ['视频播放进度达 50% 的次数']),
    ('v75', ['视频播放进度达 75% 的次数']),
    ('v95', ['视频播放进度达 95% 的次数']),
    ('v100', ['视频播放进度达 100% 的次数']),
    ('page_engagement', ['公共主页互动量']),
    ('post_reactions', ['帖子心情']),
    ('post_comments', ['帖子评论数']),
    ('post_saves', ['帖子收藏']),
    ('post_shares', ['帖子分享次数']),
    ('fb_likes', ['Facebook 获赞数']),
    ('ig_follows', ['Instagram 关注次数']),
])

# 扩展特征：名字 → (中文说明, 依赖的原始指标键, 算法, 理论上限为 1？)
#   ratio        ：键[0] ÷ 键[1]
#   fans_per_imp ：(FB获赞 + IG关注) ÷ 展示次数
#   cost_per_fan ：花费 ÷ (FB获赞 + IG关注)
# 第 4 项 = True 表示这是「比例」，> 1 说明两个源指标的**分母不是同一个**，
# 必须报警而不是照显示（实测短袖2 的 视频留存25 = 177/148 = 1.196，就是这样）。
EXT_FEATURE_SPECS = OrderedDict([
    ('全部点击倍数', ('有多少点击其实没点链接（恶意/误触越多越高）',
                 ['click_all', 'clicks'], 'ratio', False)),
    ('落地页到达率', ('点了链接真到了落地页的比例（低了 = 加载慢/被拦）',
                 ['landing_views', 'clicks'], 'ratio', True)),
    ('落地页单次浏览成本', ('每带来一个落地页浏览花多少钱',
                    ['spend', 'landing_views'], 'ratio', False)),
    ('3秒播放率', ('素材前 3 秒的钩子强度', ['video_3s', 'impressions'], 'ratio', True)),
    ('ThruPlay率', ('完整看完或看满 15 秒的比例', ['thruplay', 'impressions'], 'ratio', True)),
    ('视频留存25', ('3 秒播放里活到 25% 的比例', ['v25', 'video_3s'], 'ratio', True)),
    ('视频留存50', ('3 秒播放里活到 50% 的比例', ['v50', 'video_3s'], 'ratio', True)),
    ('视频完播率', ('3 秒播放里看到底的比例', ['v100', 'video_3s'], 'ratio', True)),
    ('互动率', ('主页互动强度（点赞/评论/分享/收藏合计）',
             ['page_engagement', 'impressions'], 'ratio', True)),
    ('评论率', ('主动评论比例（比点赞贵得多的信号）',
             ['post_comments', 'impressions'], 'ratio', True)),
    ('分享率', ('转发比例（最强的内容认同信号）',
             ['post_shares', 'impressions'], 'ratio', True)),
    ('涨粉率', ('每千次展示带来多少新粉丝',
             ['fb_likes', 'ig_follows', 'impressions'], 'fans_per_imp', True)),
    ('互动涨粉成本', ('每个新粉丝花多少钱',
                ['spend', 'fb_likes', 'ig_follows'], 'cost_per_fan', False)),
])

EXT_FEATURE_KEYS = list(EXT_FEATURE_SPECS.keys())


def ext_raw_colmap(header):
    """表头 → {原始指标键: 命中的列名 或 None}。"""
    return {k: resolve_col(header, aliases) for k, aliases in EXT_RAW_ALIASES.items()}


def rows_union_header(rows):
    """一组行的**键并集**（保持首次出现顺序）。

    ⚠️ 必须用并集，不能用 `rows[0].keys()`：不同视图覆盖的日期不一样
    （实测视频/互动视图只覆盖最后 4 天），拿第一行当表头会把这些列整块漏掉。
    """
    cols, seen = [], set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    return cols


def _isnum(s):
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _row_values(rows, colmap):
    """每行的原始指标值。列不存在、值为空、或值不是数字 → **None**（不是 0）。"""
    out = []
    for r in rows:
        d = {}
        for k, col in colmap.items():
            if not col:
                continue
            raw = r.get(col)
            v = ('' if raw is None else str(raw)).strip()
            d[k] = float(v) if (v and _isnum(v)) else None
        out.append(d)
    return out


def _paired_window(vals, keys):
    """只保留「这组键**全部**有值」的行 —— 分子分母必须同一个窗口，否则比值是假的。

    实测教训：互动广告系列的「落地页浏览量」只报了 1 天，而「链接点击量」报了 4 天；
    不限制窗口就会拿 1 天的分子去除 4 天的分母，算出来的 0.0028 纯属虚构
    （限定到同一天是 0.0317，差 11 倍）。
    """
    win = [d for d in vals if all(d.get(k) is not None for k in keys)]
    if not win:
        return None, 0
    return {k: sum(d[k] for d in win) for k in keys}, len(win)


def _safe_div(a, b):
    """不可算返回 None —— **不是 0**。分不清「没有」和「算不出」会骗人。"""
    if a is None or b is None:
        return None
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if b == 0:
        return None
    return a / b


def _eval_ext(kind, tot):
    """按算法把合计变成比值。"""
    def g(k):
        return tot.get(k)

    if kind == 'ratio':
        return _safe_div(g('num'), g('den'))
    if kind == 'fans_per_imp':
        fans = (g('fb_likes') or 0.0) + (g('ig_follows') or 0.0)
        return _safe_div(fans, g('impressions'))
    if kind == 'cost_per_fan':
        fans = (g('fb_likes') or 0.0) + (g('ig_follows') or 0.0)
        return _safe_div(g('spend'), fans)
    return None


def ext_report_by_ad(wide):
    """宽表 → {广告名: {'features','days','suspect','raw'}}。

    `days[feat]`  = 这个比值**实际用了几天**（配对窗口的行数）。
    `suspect`     = 理论上是「比例」却算出了 > 1 的特征名。
                    出现它说明两个源指标的分母不是同一个（不是我们的算错），
                    这种数字必须报出来，不能照显示。
    """
    out = OrderedDict()
    for ad, rows in wide_rows_by_ad(wide).items():
        colmap = ext_raw_colmap(rows_union_header(rows))
        vals = _row_values(rows, colmap)
        feats, days = {}, {}
        for name, (_desc, keys, kind, max1) in EXT_FEATURE_SPECS.items():
            tot, n = _paired_window(vals, keys)
            if tot is None:
                feats[name] = None
                days[name] = 0
                continue
            if kind == 'ratio':
                tot = {'num': tot[keys[0]], 'den': tot[keys[1]]}
            feats[name] = _eval_ext(kind, tot)
            days[name] = n
        suspect = [k for k, v in feats.items()
                   if v is not None and EXT_FEATURE_SPECS[k][3] and v > 1.0 + 1e-9]
        out[ad] = {'features': feats, 'days': days,
                   'suspect': suspect, 'raw': _sum_raw(vals, colmap)}
    return out


def _sum_raw(vals, colmap):
    """每列在整个可见区间上的合计（缺列/缺值的行按 0 累加）—— 只用于展示。"""
    tot = {}
    for k in colmap:
        if not colmap.get(k):
            continue
        tot[k] = sum(d[k] for d in vals if d.get(k) is not None)
    return tot


def ext_features_by_ad(wide):
    """{广告名: {特征名: 值或 None}} —— 只取比值，不管支撑天数。"""
    rep = ext_report_by_ad(wide)
    return OrderedDict((ad, r['features']) for ad, r in rep.items())


def ext_window_days_by_ad(wide):
    """{广告名: {特征名: 支撑天数}}。"""
    rep = ext_report_by_ad(wide)
    return OrderedDict((ad, r['days']) for ad, r in rep.items())


def ext_coverage(wide):
    """扩展特征里**有多少条广告真算得出来**（覆盖率），用于诚实汇报。"""
    rep = ext_report_by_ad(wide)
    n = len(rep)
    cov = OrderedDict()
    for k in EXT_FEATURE_KEYS:
        cov[k] = sum(1 for r in rep.values() if r['features'].get(k) is not None)
    return cov, n


# ==================== 6. 缺口清单（该再导哪几份）====================

def gap_report(exports):
    """已有哪些视图、缺哪些视图、缺了会失去什么。"""
    have = OrderedDict()
    for e in exports:
        have.setdefault(e.view, []).append(e)
    present = [v for v in ALL_VIEWS if v in have]
    missing = [v for v in ALL_VIEWS if v not in have]
    return {
        'present': present,
        'missing': missing,
        'unknown': have.get('unknown', []),
        'loss': [(v, VIEW_LOSS.get(v, '')) for v in missing],
    }


def budget_anomaly(exports):
    """检查「广告组预算」列是不是数字。

    实测：平台在部分广告组上把文案（如「使用广告组预算」）写进这一列，类型列写「0」。
    此时 `fnum()` 会把它读成 0 ⇒ 花费率、预算档这两个特征**静默失效**。
    返回 [{'ad':.., 'value':.., 'file':..}, ...]（非数值的取值）。
    """
    bad = []
    seen = set()
    for e in exports:
        col = resolve_col(e.header, ['广告组预算', '广告系列预算', '预算'])
        if not col:
            continue
        for r in e.rows:
            v = (r.get(col) or '').strip()
            if not v:
                continue
            try:
                float(v)
                continue
            except ValueError:
                pass
            ad = str(r.get(e.group_col) or '') if e.group_col else ''
            k = (ad, v)
            if k in seen:
                continue
            seen.add(k)
            bad.append({'ad': ad, 'value': v, 'file': e.name, 'col': col})
    return bad


def overview(paths=None, folder=None, pattern='*.csv'):
    """一步到位：扫文件 → 识别 → 去重 → 合并 → 出全部报告。CLI 与测试共用。"""
    if paths is None:
        paths = scan_dir(pattern, folder or D)
    exports = [load_export(p) for p in paths]
    exports = [e for e in exports if e.n]
    groups, chosen, dropped = dedupe_exports(exports)
    wide, mrep = merge_exports(chosen)
    ext_rep = ext_report_by_ad(wide)
    return {
        'exports': exports,
        'groups': groups,
        'chosen': chosen,
        'dropped': dropped,
        'wide': wide,
        'merge': mrep,
        'gap': gap_report(exports),
        'budget_anomaly': budget_anomaly(exports),
        'ext_report': ext_rep,
        'ext_by_ad': OrderedDict((ad, r['features']) for ad, r in ext_rep.items()),
        'ext_days_by_ad': OrderedDict((ad, r['days']) for ad, r in ext_rep.items()),
    }


if __name__ == '__main__':
    print(__doc__)
