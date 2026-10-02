# -*- coding: utf-8 -*-
"""补护栏值多少钱（口径修正版）。

四条护栏，逐级叠加，单一变量：
    动作映射：stop→5天净 0 ｜ up→加预算_净 ｜ observe→不动_净
    P  现状          ：照 AI 的动作
    Q1 ＋止损线      ：净<=0 → 强制 stop（其余照 AI）
    Q2 ＋止损＋禁杀线 ：Q1 基础上，净>0 的广告不许被判 stop（改判 observe）
    Q3 只加禁杀线     ：照 AI，但净>0 不许 stop

⚠️ P 侧口径修正：AI 说"观察"的广告必须按「不动」算净，不能按真值动作算，
   否则等于替它把该亏的账抹成 0，会高估现状。
"""
import csv
import io
import os

D = os.path.dirname(os.path.abspath(__file__))
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_AI = os.path.join(D, 'AI逐条结论-v11.csv')
P_OUT = os.path.join(D, '_opt_gain_out.txt')

L = []


def say(s=''):
    L.append(s)


def num(x):
    try:
        return float(str(x).strip())
    except Exception:
        return 0.0


truth = {}
with io.open(P_TRUTH, encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        truth[r['广告']] = r

ai = {}
with io.open(P_AI, encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        ai[r['对象']] = r

names = [n for n in truth if n in ai]
STOPPED = ('pause', 'decrease_budget')


def base_act(n):
    a = ai[n]['AI动作']
    if a in STOPPED:
        return 'stop'
    return 'up' if a == 'increase_budget' else 'observe'


def pay(t, act):
    if act == 'stop':
        return 0.0
    return num(t['加预算_净']) if act == 'up' else num(t['不动_净'])


def plan(n, mode):
    t = truth[n]
    net = num(t['前5天净'])
    act = base_act(n)
    if mode == 'P':
        return act
    if mode in ('Q1', 'Q2') and net <= 0:
        return 'stop'
    if mode in ('Q2', 'Q3') and net > 0 and act == 'stop':
        return 'observe'
    return act


MODES = [('P', '现状（照 AI 的动作）'),
         ('Q1', '＋止损线：可见净<=0 → 停'),
         ('Q3', '只加禁杀线：可见净>0 不许停'),
         ('Q2', '＋止损线 ＋禁杀线（合起来）')]

res = {}
say('=' * 76)
say('护栏值多少钱（模拟数据 100 条 · 后 5 天净收益 · 事后账）')
say('=' * 76)
say()
for m, label in MODES:
    tot = sum(pay(truth[n], plan(n, m)) for n in names)
    res[m] = tot
    say('  %-4s %-32s 后 5 天合计净 %+10.2f' % (m, label, tot))
say()
say('  ➜ Q1 相对现状 %+0.2f 美元' % (res['Q1'] - res['P']))
say('  ➜ Q3 相对现状 %+0.2f 美元' % (res['Q3'] - res['P']))
say('  ➜ Q2 相对现状 %+0.2f 美元  ← 两条护栏一起上' % (res['Q2'] - res['P']))

say()
say('=' * 76)
say('逐条差异（Q2 相对 P，只列有变化的）')
say('=' * 76)
say('  %-42s %-9s %-9s %10s' % ('广告', 'P', 'Q2', '净变化'))
tot_d = 0.0
for n in names:
    a, b = plan(n, 'P'), plan(n, 'Q2')
    if a != b:
        t = truth[n]
        d = pay(t, b) - pay(t, a)
        tot_d += d
        tag = ''
        if a == 'stop':
            tag = '← 误杀被救回'
        elif b == 'stop':
            tag = '← 漏杀被兜住'
        say('  %-42s %-9s %-9s %+10.2f  [真值=%s] %s'
            % (n[:42], a, b, d, t['正确动作'], tag))
say('  ' + '-' * 72)
say('  %-42s %-9s %-9s %+10.2f' % ('合计', '', '', tot_d))

say()
say('=' * 76)
say('两类错的钱（按真值动作计）')
say('=' * 76)
miss = [n for n in names if truth[n]['正确动作'] == '暂停' and base_act(n) != 'stop']
over = [n for n in names if truth[n]['正确动作'] != '暂停' and base_act(n) == 'stop']
say('  漏杀 %d 条（真值停、AI 没停）→ 若补上止损线，后 5 天多赚 %+.2f'
    % (len(miss), sum(-pay(truth[n], 'observe') for n in miss)))
say('  误杀 %d 条（AI 停、真值不是停）→ 后 5 天少赚 %+.2f'
    % (len(over), sum(pay(truth[n], 'observe') - pay(truth[n], 'stop') for n in over)))
for n in over:
    say('     %-42s 真值=%-6s 不停本可 %+9.2f' % (n[:42], truth[n]['正确动作'],
                                                pay(truth[n], 'observe')))

say()
say('=' * 76)
say('附：各方案的总分（准确率，与真值逐条比）')
say('=' * 76)
CN = {'stop': '暂停', 'observe': '观察', 'up': '加预算'}


def acc(pick):
    hit = sum(1 for n in names if CN[pick(n)] == truth[n]['正确动作'])
    return hit / len(names) * 100


def q4(n):
    """完全用规则决策（不看 AI）：止损线 + 加钱提名线。"""
    t = truth[n]
    net = num(t['前5天净'])
    if net <= 0:
        return 'stop'
    if num(t['_顶格率5']) >= 0.90 and num(t['_频次5']) < 1.35:
        return 'up'
    return 'observe'


say('  P  现状（纯 AI）                        %.1f%%' % acc(lambda n: plan(n, 'P')))
say('  Q1 ＋止损线                             %.1f%%' % acc(lambda n: plan(n, 'Q1')))
say('  Q2 ＋止损线 ＋禁杀线                      %.1f%%' % acc(lambda n: plan(n, 'Q2')))
say('  Q4 规则全接管（止损线＋加钱提名线）           %.1f%%   ← 不用 AI' % acc(q4))
say('     （对照：什么都不做全判「暂停」= %.1f%%）'
    % (sum(1 for n in names if truth[n]['正确动作'] == '暂停') / len(names) * 100))

with io.open(P_OUT, 'w', encoding='utf-8') as f:
    f.write('\n'.join(L))
