# -*- coding: utf-8 -*-
"""_v11_ceiling_up.py —— 只问一件事：**「该加预算 vs 其余」这个二分类，可见数据的上限是多少？**

为什么要单独算：
  三分类天花板 75% 里，大部分分数来自「暂停」这个好判的类（花费+零购买，肉眼可见）。
  但真正决定工具能不能用的，是「该加」这一类的可判性。
  如果二分类上限本来就只有 55%，那 AI 的 34.5% 召回是"接近尽力"；如果有 80%，那就是"没干活"。

输出：_v11_ceiling_up_out.txt
"""
import csv
import io
import os
from collections import defaultdict

from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier

D = os.path.dirname(os.path.abspath(__file__))
P_DATA = os.path.join(D, '模拟广告数据-100广告10天-v5.csv')
P_TRUTH = os.path.join(D, '真值表-v5.csv')

L = []
def say(s=''):
    L.append(s)


def f(x):
    try:
        return float(x or 0)
    except Exception:
        return 0.0


truth = {r['广告']: r for r in csv.DictReader(io.StringIO(open(P_TRUTH, encoding='utf-8-sig').read()))}
rows = list(csv.DictReader(io.StringIO(open(P_DATA, encoding='utf-8-sig').read())))
g = defaultdict(list)
for r in rows:
    g[r['广告系列名称']].append(r)

X, y, names = [], [], []
for name, rs in g.items():
    if name not in truth:
        continue
    rs = sorted(rs, key=lambda z: z['报告开始日期'])
    d1, d5 = rs[0], rs[-1]
    spend = sum(f(r['已花费金额 (USD)']) for r in rs)
    rev = sum(f(r['购物转化价值']) for r in rs)
    pur = sum(f(r['购物次数']) for r in rs)
    imp = sum(f(r['展示次数']) for r in rs)
    clk = sum(f(r['链接点击量']) for r in rs)
    atc = sum(f(r['加入购物车次数']) for r in rs)
    ic = sum(f(r['结账发起次数']) for r in rs)
    pay = sum(f(r['添加支付信息']) for r in rs)
    reach1, reach5 = f(d1['覆盖人数']), f(d5['覆盖人数'])
    budget = f(rs[0]['广告组预算'])
    nd = len(rs)
    feat = [
        rev / spend if spend else 0, spend / (budget * nd) if budget else 0,
        pur / nd, spend / pur if pur else 999, rev / pur if pur else 0,
        f(d1['频次']), f(d5['频次']), f(d5['频次']) - f(d1['频次']),
        (reach5 - reach1) / max(1.0, reach1) * 100, reach1, reach5,
        spend / imp * 1000 if imp else 0,
        (f(d5['CPM（千次展示费用） (USD)']) - f(d1['CPM（千次展示费用） (USD)']))
        / max(1e-9, f(d1['CPM（千次展示费用） (USD)'])) * 100,
        (f(d5['链接点击量']) / max(1, f(d5['展示次数'])) - f(d1['链接点击量']) / max(1, f(d1['展示次数'])))
        / max(1e-9, f(d1['链接点击量']) / max(1, f(d1['展示次数']))) * 100,
        atc / clk if clk else 0, ic / atc if atc else 0, pay / ic if ic else 0,
        pur / pay if pay else 0, budget,
        sum(f(r['购物次数']) for r in rs[:3]) / pur if pur else 0,
    ]
    X.append(feat)
    y.append(1 if truth[name]['正确动作'] == '加预算' else 0)
    names.append(name)

say('=== 「该加预算 vs 其余」二分类可解性（v5 数据，%d 条）===' % len(X))
say('  正类（真值=加预算）: %d 条 (%.0f%%)  → 恒判负类的基线 = %.0f%%'
    % (sum(y), sum(y) / len(y) * 100, (1 - sum(y) / len(y)) * 100))
say()
cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=7)
say('  %-14s %8s %8s %10s' % ('模型', '准确率', 'AUC', '正类召回'))
say('  ' + '-' * 44)
for label, mdl in [('逻辑回归', make_pipeline(StandardScaler(), LogisticRegression(max_iter=20000, class_weight='balanced'))),
                   ('决策树 深3', DecisionTreeClassifier(max_depth=3, random_state=1, class_weight='balanced')),
                   ('决策树 深5', DecisionTreeClassifier(max_depth=5, random_state=1, class_weight='balanced')),
                   ('随机森林', RandomForestClassifier(n_estimators=400, random_state=1, class_weight='balanced'))]:
    acc = cross_val_score(mdl, X, y, cv=cv, scoring='accuracy').mean() * 100
    auc = cross_val_score(mdl, X, y, cv=cv, scoring='roc_auc').mean()
    rec = cross_val_score(mdl, X, y, cv=cv, scoring='recall').mean() * 100
    say('  %-14s %7.1f%% %8.3f %9.1f%%' % (label, acc, auc, rec))
say()
say('  读法：AUC ≈ 0.5 表示「可见数据里没有信息，纯运气」；')
say('        AUC ≈ 0.7 表示「有点信息但很弱」；AUC > 0.85 才算「能判」。')

with io.open(os.path.join(D, '_v11_ceiling_up_out.txt'), 'w', encoding='utf-8') as fp:
    fp.write('\n'.join(L))
print('\n'.join(L))
