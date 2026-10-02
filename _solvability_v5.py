# -*- coding: utf-8 -*-
"""_solvability_v5.py —— 先验证「这道题有没有解」，再拿去考 AI。（v5 数据版）

规矩（2026-10-02 定死）：
    天花板 < 45%  → 题无信息，不许喂给 AI
    天花板 > 85%  → 题太白给，AI 得高分不说明能力
    45%~85%      → 合格

v5 与 v4 的特征差异：频次不再是主要信号（真实数据里它太稳），
改为把「覆盖衰减速度 / CPM 漂移 / 顶格率」放进特征。

输出：_solvability_v5_out.txt
"""
import csv
import io
import os
from collections import Counter, defaultdict

from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier

D = os.path.dirname(os.path.abspath(__file__))
P_DATA = os.path.join(D, '模拟广告数据-100广告10天-v5.csv')
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_OUT = os.path.join(D, '_solvability_v5_out.txt')

L = []
def say(s=''):
    L.append(s)


def fnum(x):
    try:
        return float(x)
    except Exception:
        return 0.0


truth = {r['广告']: r for r in csv.DictReader(io.StringIO(open(P_TRUTH, encoding='utf-8-sig').read()))}
rows = list(csv.DictReader(io.StringIO(open(P_DATA, encoding='utf-8-sig').read())))

g = defaultdict(list)
for r in rows:
    g[r['广告系列名称']].append(r)

names, X, y = [], [], []
for name, rs in g.items():
    rs = sorted(rs, key=lambda z: z['报告开始日期'])
    if name not in truth:
        continue
    d1, d5 = rs[0], rs[-1]
    spend = sum(fnum(r['已花费金额 (USD)']) for r in rs)
    rev = sum(fnum(r['购物转化价值']) for r in rs)
    pur = sum(fnum(r['购物次数']) for r in rs)
    imp = sum(fnum(r['展示次数']) for r in rs)
    clk = sum(fnum(r['链接点击量']) for r in rs)
    atc = sum(fnum(r['加入购物车次数']) for r in rs)
    ic = sum(fnum(r['结账发起次数']) for r in rs)
    pay = sum(fnum(r['添加支付信息']) for r in rs)
    reach1, reach5 = fnum(d1['覆盖人数']), fnum(d5['覆盖人数'])
    budget = fnum(rs[0]['广告组预算'])
    nd = len(rs)
    ctr1 = fnum(d1['链接点击量']) / max(1, fnum(d1['展示次数']))
    ctr5 = fnum(d5['链接点击量']) / max(1, fnum(d5['展示次数']))
    imp1, imp5 = fnum(d1['展示次数']), fnum(d5['展示次数'])
    cpm1 = fnum(d1['CPM（千次展示费用） (USD)'])
    cpm5 = fnum(d5['CPM（千次展示费用） (USD)'])

    feat = {
        '花费': spend, '收入': rev, '购买': pur, 'ROAS': rev / spend if spend else 0,
        '花费率': spend / (budget * nd) if budget else 0,
        'CTR首': ctr1 * 100, 'CTR末': ctr5 * 100, 'CTR斜率%': (ctr5 - ctr1) / max(1e-9, ctr1) * 100,
        '曝光斜率%': (imp5 - imp1) / max(1.0, imp1) * 100,
        '覆盖首': reach1, '覆盖末': reach5,
        '覆盖斜率%': (reach5 - reach1) / max(1.0, reach1) * 100,
        'CPM首': cpm1, 'CPM末': cpm5, 'CPM斜率%': (cpm5 - cpm1) / max(1e-9, cpm1) * 100,
        '频次首': fnum(d1['频次']), '频次末': fnum(d5['频次']),
        '频次增幅': fnum(d5['频次']) - fnum(d1['频次']),
        'CPC': spend / clk if clk else 0,
        '加购率': atc / clk if clk else 0, '结账率': ic / atc if atc else 0,
        '支付率': pay / ic if ic else 0, '购买率': pur / pay if pay else 0,
        '日均购买': pur / nd, '单均成本': spend / pur if pur else 999,
        '客单价': rev / pur if pur else 0,
        '预算': budget, '是系列预算': 1.0 if '系列' in rs[0]['广告组预算类型'] else 0.0,
        '前3天购买占比': sum(fnum(r['购物次数']) for r in rs[:3]) / pur if pur else 0,
        '首单在第几天': next((i + 1 for i, r in enumerate(rs) if fnum(r['购物次数']) > 0), nd + 1),
    }
    names.append(name)
    X.append([feat[k] for k in feat])
    y.append(truth[name]['正确动作'])

keys = list(feat.keys())
say('=== 可解性验证 v5（%d 条广告，%d 维可见特征）===' % (len(X), len(keys)))
say('特征：%s' % '、'.join(keys))
say()

base = Counter(y)
say('=== 基线（不做任何判断）===')
for k, v in base.most_common():
    say('  恒定判「%s」→ %.1f%%' % (k, v / len(y) * 100))
say('  随机瞎猜（3 类均匀）→ 33.3%')
say()

cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=7)
say('=== 模型天花板（5 折交叉验证，只用可见列）===')
res = []
for label, mdl in [('逻辑回归', make_pipeline(StandardScaler(), LogisticRegression(max_iter=20000))),
                   ('决策树 深3', DecisionTreeClassifier(max_depth=3, random_state=1)),
                   ('决策树 深5', DecisionTreeClassifier(max_depth=5, random_state=1)),
                   ('随机森林', RandomForestClassifier(n_estimators=400, random_state=1)),
                   ('随机森林 深6', RandomForestClassifier(n_estimators=400, max_depth=6, random_state=1))]:
    s = cross_val_score(mdl, X, y, cv=cv, scoring='accuracy')
    res.append((label, s.mean() * 100, s.std() * 100))
    say('  %-12s %.1f%%  (±%.1f)' % (label, s.mean() * 100, s.std() * 100))

best = max(r[1] for r in res)
say()
say('=== 结论 ===')
if best < 45:
    verdict = '❌ 天花板 %.1f%% 太低 → 题无信息，**不许喂给 AI**（会重演 v3 的误判）' % best
elif best > 85:
    verdict = '⚠️ 天花板 %.1f%% 太高 → 题太白给，AI 得高分不说明能力，建议加噪声' % best
else:
    verdict = '✅ 天花板 %.1f%% 落在 45~85%% 区间 → **题有解且不易**，可以拿去考 AI' % best
say(verdict)

say()
say('=== 人工规则基线（模拟老投手直觉）===')
rules = {
    '规则A：ROAS>2 且 覆盖没缩 →加；ROAS<1.3 →停；否则观察': lambda d: (
        '加预算' if d['ROAS'] > 2 and d['覆盖斜率%'] > -8
        else ('暂停' if d['ROAS'] < 1.3 else '观察')),
    '规则B：ROAS>1.8 且 频次增幅<0.25 →加；ROAS<1.5 →停；否则观察': lambda d: (
        '加预算' if d['ROAS'] > 1.8 and d['频次增幅'] < 0.25
        else ('暂停' if d['ROAS'] < 1.5 else '观察')),
    '规则C：ROAS>2 且 CPM不涨 →加；花费率<0.9且ROAS<1.5 →停；否则观察': lambda d: (
        '加预算' if d['ROAS'] > 2 and d['CPM斜率%'] < 5
        else ('暂停' if d['花费率'] < 0.9 and d['ROAS'] < 1.5 else '观察')),
}
feats = [{k: v for k, v in zip(keys, row)} for row in X]
for label, fn in rules.items():
    ok = sum(1 for d, t in zip(feats, y) if fn(d) == t)
    say('  %-56s %.1f%%' % (label, ok / len(y) * 100))

with io.open(P_OUT, 'w', encoding='utf-8') as fp:
    fp.write('\n'.join(L))
print('\n'.join(L))
