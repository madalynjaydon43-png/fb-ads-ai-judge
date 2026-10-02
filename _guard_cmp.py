# -*- coding: utf-8 -*-
"""「该关没关」优化方案四方对照。

  ① 纯 AI                    —— AI逐条结论-v11.csv
  ② AI ＋ 提示词护栏（止损+禁杀）—— AI逐条结论-v11_note.csv
  ③ AI ＋ 代码护栏              —— 在①之上：净<=0 强制停、净>0 强制不许停
  ④ 纯规则（不用 AI）           —— 止损线 + 加钱提名线

统一口径：钱 = Σ 后 5 天净（stop→0 / up→加预算_净 / observe→不动_净）
"""
import csv
import io
import os

D = os.path.dirname(os.path.abspath(__file__))
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_OUT = os.path.join(D, '_guard_cmp_out.txt')

L = []


def say(s=''):
    L.append(s)


def num(x):
    try:
        return float(str(x).strip())
    except Exception:
        return 0.0


def load_ai(fn):
    p = os.path.join(D, fn)
    if not os.path.exists(p):
        return None
    d = {}
    with io.open(p, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            d[r['对象']] = r
    return d


truth = {}
with io.open(P_TRUTH, encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        truth[r['广告']] = r

AI0 = load_ai('AI逐条结论-v11.csv')
AI1 = load_ai('AI逐条结论-v11_note.csv')
names = [n for n in truth if AI0 and n in AI0]
STOPPED = ('pause', 'decrease_budget')


def act_raw(ai, n):
    a = ai[n]['AI动作']
    if a in STOPPED:
        return 'stop'
    return 'up' if a == 'increase_budget' else 'observe'


def pay(t, act):
    if act == 'stop':
        return 0.0
    return num(t['加预算_净']) if act == 'up' else num(t['不动_净'])


def netof(n):
    return num(truth[n]['前5天净'])


def guard_code(n, act):
    if netof(n) <= 0:
        return 'stop'
    if act == 'stop':
        return 'observe'
    return act


def rule_all(n):
    t = truth[n]
    if netof(n) <= 0:
        return 'stop'
    if num(t['_顶格率5']) >= 0.90 and num(t['_频次5']) < 1.35:
        return 'up'
    return 'observe'


PLANS = []
if AI0:
    PLANS.append(('① 纯 AI（现状）', lambda n: act_raw(AI0, n)))
if AI1:
    PLANS.append(('② AI ＋ 提示词护栏', lambda n: act_raw(AI1, n)))
if AI0:
    PLANS.append(('③ AI ＋ 代码护栏', lambda n: guard_code(n, act_raw(AI0, n))))
PLANS.append(('④ 纯规则（不用 AI）', rule_all))

CN = {'stop': '暂停', 'observe': '观察', 'up': '加预算'}
REV = {'暂停': 'stop', '观察': 'observe', '加预算': 'up'}

say('=' * 78)
say('「该关没关」优化方案对照（100 条 · 后 5 天净收益口径）')
say('=' * 78)
say()
say('  %-22s %8s %9s %9s %11s %11s' %
    ('方案', '总分', '关停召回', '关停精度', '漏杀少省$', '误杀少赚$'))
say('  ' + '-' * 74)

detail = {}
for label, pick in PLANS:
    hit = 0
    tp = fp = fn_ = 0
    money = 0.0
    miss_money = over_money = 0.0
    rows = []
    for n in names:
        act = pick(n)
        money += pay(truth[n], act)
        if CN[act] == truth[n]['正确动作']:
            hit += 1
        if act == 'stop' and truth[n]['正确动作'] == '暂停':
            tp += 1
        elif act == 'stop':
            fp += 1
            over_money += pay(truth[n], 'observe') - pay(truth[n], 'stop')
        elif truth[n]['正确动作'] == '暂停':
            fn_ += 1
            miss_money += -pay(truth[n], 'observe')
        rows.append((n, act))
    detail[label] = rows
    rec = tp / (tp + fn_) * 100 if tp + fn_ else 0
    pre = tp / (tp + fp) * 100 if tp + fp else 0
    say('  %-22s %7.1f%% %8.0f%% %8.0f%% %11.2f %11.2f'
        % (label, hit / len(names) * 100, rec, pre, -miss_money, -over_money))

say()
say('  （「漏杀少省$」= 该停没停导致多亏的钱，越接近 0 越好；')
say('    「误杀少赚$」= 不该停却停了导致少赚的钱，同样越接近 0 越好）')
say()
say('  各方案后 5 天合计净收益：')
for label, pick in PLANS:
    say('    %-22s %+10.2f' % (label, sum(pay(truth[n], pick(n)) for n in names)))

if AI1:
    say()
    say('=' * 78)
    say('② 提示词护栏 之后，还漏/还误的逐条')
    say('=' * 78)
    say()
    say('  仍然漏杀（真值=暂停，AI 没停）：')
    cnt = 0
    for n in names:
        a = act_raw(AI1, n)
        if truth[n]['正确动作'] == '暂停' and a != 'stop':
            cnt += 1
            say('    %-44s AI=%-8s 可见净 %+8.2f' % (n[:44], a, netof(n)))
    if not cnt:
        say('    （无）')
    say()
    say('  仍然误杀（AI 判停，真值不是暂停）：')
    cnt = 0
    for n in names:
        a = act_raw(AI1, n)
        if a == 'stop' and truth[n]['正确动作'] != '暂停':
            cnt += 1
            say('    %-44s 真值=%-6s 可见净 %+8.2f ｜ 不停本可 %+9.2f'
                % (n[:44], truth[n]['正确动作'], netof(n), pay(truth[n], 'observe')))
    if not cnt:
        say('    （无）')

with io.open(P_OUT, 'w', encoding='utf-8') as f:
    f.write('\n'.join(L))
