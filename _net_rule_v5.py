# -*- coding: utf-8 -*-
"""
对照两种判据：
  判据甲（用户版）：末 2 天零单 → 不加
  判据乙（净版）  ：可见 5 天净 <= 0 → 停；净 > 0 且 顶格 + 频次低 → 加；其余观察
看谁更接近真相。
"""
import csv
import io
import os
from collections import defaultdict

D = os.path.dirname(os.path.abspath(__file__))
P_DATA = os.path.join(D, '模拟广告数据-100广告10天-v5.csv')
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_OUT = os.path.join(D, '_net_rule_out.txt')

L = []


def say(s=''):
    L.append(s)


def num(x, d=0.0):
    try:
        return float(str(x).strip())
    except Exception:
        return d


days = defaultdict(dict)
with io.open(P_DATA, encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        days[r['广告系列名称']][r['报告开始日期']] = r

truth = {}
with io.open(P_TRUTH, encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        truth[r['广告']] = r


def agg(name):
    rows = [days[name][d] for d in sorted(days[name].keys())]
    spend = sum(num(r['已花费金额 (USD)']) for r in rows)
    rev = sum(num(r['购物转化价值']) for r in rows)
    pur = sum(num(r['购物次数']) for r in rows)
    clk = sum(num(r['链接点击量']) for r in rows)
    # 顶格率：花掉的钱 / 预算上限（广告组预算）
    bud = num(rows[0]['广告组预算'])
    fill = sum(num(r['已花费金额 (USD)']) for r in rows) / (bud * len(rows)) if bud > 0 else 0
    freq5 = num(rows[-1]['频次'])
    zr = 0
    for r in reversed(rows):
        if num(r['购物次数']) == 0:
            zr += 1
        else:
            break
    return dict(net=rev - spend, spend=spend, rev=rev, pur=pur, clk=clk,
                fill=fill, freq5=freq5, roas=(rev / spend if spend > 0 else 0), zero_run=zr,
                rows=rows)


A = {n: agg(n) for n in truth}
names = list(truth.keys())


def score(rule, label):
    ok = 0
    cm = defaultdict(int)
    for n in names:
        pred = rule(n, A[n])
        act = truth[n]['正确动作']
        cm[(act, pred)] += 1
        if pred == act:
            ok += 1
    say('【%s】准确率 %d/%d = %.1f%%' % (label, ok, len(names), ok / len(names) * 100))
    say('  真值\\预测   ' + ''.join('%10s' % a for a in ('加预算', '观察', '暂停')))
    for a in ('加预算', '观察', '暂停'):
        say('  %-10s' % a + ''.join('%10d' % cm[(a, p)] for p in ('加预算', '观察', '暂停')))
    rec = {}
    for a in ('加预算', '观察', '暂停'):
        tot = sum(cm[(a, p)] for p in ('加预算', '观察', '暂停'))
        rec[a] = cm[(a, a)] / tot * 100 if tot else 0
    say('  各类命中：加预算 %.0f%% / 观察 %.0f%% / 暂停 %.0f%%' %
        (rec['加预算'], rec['观察'], rec['暂停']))
    say()
    return ok / len(names) * 100


say('=' * 78)
say('对照：判据甲（末2天零单就不加） vs 判据乙（看可见净的正负）')
say('=' * 78)
say()

BASE = sum(1 for n in names if truth[n]['正确动作'] == '暂停') / len(names) * 100
say('参考：什么都不想，全判「暂停」= %.1f%%' % BASE)
say()


def rule_jia(n, a):
    """人类直觉版：用「最近有没有出单」当主力判据。"""
    if a['zero_run'] >= 2:
        return '暂停'                              # 连着两天不出单 → 停
    if a['fill'] >= 0.90 and a['freq5'] < 1.35:
        return '加预算'                            # 有单 + 预算花得出去 → 加
    return '观察'


def rule_yi(n, a):
    """净版：用「看得见的钱是赚是亏」当主力判据。"""
    if a['net'] <= 0:
        return '暂停'                              # 可见窗口在亏 → 停
    if a['fill'] >= 0.90 and a['freq5'] < 1.35 and a['roas'] > 1.3:
        return '加预算'                            # 在赚 + 花得出去 + 受众没看腻 → 加
    return '观察'


score(rule_jia, '判据甲（零单版）：末2天零单→停；有单+顶格+频次低→加；其余观察')
score(rule_yi, '判据乙（净版）：可见净<=0→停；净>0+顶格+频次低+ROAS>1.3→加')

# 判据乙拆开看：止损这一半单独有多准
say('-' * 78)
say('判据乙拆两半看（因为"停"和"加"是两回事）')
say('-' * 78)
say()
ok_kill = tot_kill = 0
ok_up = tot_up = 0
for n in names:
    a = A[n]
    act = truth[n]['正确动作']
    if a['net'] <= 0:                     # 规则说停
        tot_kill += 1
        if act == '暂停':
            ok_kill += 1
    else:
        if a['fill'] >= 0.90 and a['freq5'] < 1.35 and a['roas'] > 1.3:
            tot_up += 1
            if act == '加预算':
                ok_up += 1
say('  「可见净<=0 → 停」：报出 %d 条，其中真值确实该停 %d 条 = 精度 %.0f%%' %
    (tot_kill, ok_kill, ok_kill / tot_kill * 100 if tot_kill else 0))
say('  「净>0+顶格+频次低 → 加」：报出 %d 条，其中真值该加 %d 条 = 精度 %.0f%%' %
    (tot_up, ok_up, ok_up / tot_up * 100 if tot_up else 0))
say()

# 卫衣 / 内衣 两条在这两套判据下的结果
say('-' * 78)
say('你问的那两条，两套判据分别怎么说')
say('-' * 78)
say()
for nm in ('新销量广告系列-0923-卫衣1-1-新 - 广告副本',
           '新互动广告系列-0923-内衣2-1-新'):
    if nm not in A:
        say('  (缺 %s)' % nm)
        continue
    a = A[nm]
    t = truth[nm]
    say('  %s' % nm)
    say('    可见 5 天：花费 %.2f / 收入 %.2f / 净 %+.2f / 出单 %d / 顶格率 %.3f / 频次 %.3f / 末段零单 %d 天' %
        (a['spend'], a['rev'], a['net'], a['pur'], a['fill'], a['freq5'], a['zero_run']))
    say('    判据甲（看零单）→ %s' % ('不加' if a['zero_run'] >= 2 else '（零单不足2天，不触发）'))
    say('    判据乙（看净）  → %s' % rule_yi(nm, a))
    say('    真值            → %s' % t['正确动作'])
    say()

with io.open(P_OUT, 'w', encoding='utf-8') as f:
    f.write('\n'.join(L))
print('OK', P_OUT)
