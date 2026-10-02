# -*- coding: utf-8 -*-
"""「规则比 AI 好？」—— 拆开每一类看，谁赢在哪、赢得干不干净。

背景：上一轮四方对照里「纯规则」80% 赢过「纯 AI」57%。但这两把尺子未必是同一件事：
  · 「暂停」这个标签 = 可见窗口净额 < -阈值      ← 答案写在可见数据里
  · 「加预算」这个标签 = 未来放量净增（含隐藏的受众饱和度）← 答案不在可见数据里
规则 ④ 恰好第一条就是「可见净<=0 → 停」，等于把标签的生成公式抄了一遍。
本脚本按类拆开，看规则的优势有多少来自「同义反复」。
"""
import csv
import io
import os

D = os.path.dirname(os.path.abspath(__file__))
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_AI = os.path.join(D, 'AI逐条结论-v11.csv')
P_OUT = os.path.join(D, '_who_wins_out.txt')

L = []


def say(s=''):
    L.append(s)


def num(x, d=0.0):
    try:
        return float(str(x).strip())
    except Exception:
        return d


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


def ai_act(n):
    a = ai[n]['AI动作']
    if a in STOPPED:
        return '暂停'
    return '加预算' if a == 'increase_budget' else '观察'


def rule_act(n):
    t = truth[n]
    net = num(t['前5天净'])
    if net <= 0:
        return '暂停'                       # 止损线
    if num(t['_顶格率5']) >= 0.90 and num(t['_频次5']) < 1.35:
        return '加预算'                     # 加钱提名线
    return '观察'


def rule_no_net(n):
    """把止损线拿掉（不用净额），看规则还剩多少本事。"""
    t = truth[n]
    if num(t['_顶格率5']) >= 0.90 and num(t['_频次5']) < 1.35:
        return '加预算'
    return '观察'


CLASSES = ('暂停', '加预算', '观察')
CN = {'暂停': '暂停', '加预算': '加预算', '观察': '观察'}

say('=' * 78)
say('「规则 vs AI」按类拆开（100 条 · 真值表-v5）')
say('=' * 78)
say()
say('  类       真值条数   AI对   AI准确率   规则对   规则准确率   差距')
say('  ' + '-' * 72)
tot_ai = tot_ru = 0
for c in CLASSES:
    sub = [n for n in names if truth[n]['正确动作'] == c]
    a = sum(1 for n in sub if ai_act(n) == c)
    r = sum(1 for n in sub if rule_act(n) == c)
    tot_ai += a
    tot_ru += r
    say('  %-8s %6d %7d %8.1f%% %7d %9.1f%% %+8.1f'
        % (c, len(sub), a, a / len(sub) * 100 if sub else 0,
           r, r / len(sub) * 100 if sub else 0,
           (r - a) / len(sub) * 100 if sub else 0))
say('  ' + '-' * 72)
say('  %-8s %6d %7d %8.1f%% %7d %9.1f%% %+8.1f'
    % ('总体', len(names), tot_ai, tot_ai / len(names) * 100,
       tot_ru, tot_ru / len(names) * 100,
       (tot_ru - tot_ai) / len(names) * 100))

say()
say('=' * 78)
say('关键一：规则赢的那 23 分，有多少来自「照抄标签定义」？')
say('=' * 78)
say()
sub_stop = [n for n in names if truth[n]['正确动作'] == '暂停']
r_stop = sum(1 for n in sub_stop if rule_act(n) == '暂停')
a_stop = sum(1 for n in sub_stop if ai_act(n) == '暂停')
gain_stop = r_stop - a_stop
gain_all = tot_ru - tot_ai
say('  「暂停」类：规则 %d/%d = %.1f%%，AI %d/%d = %.1f%%'
    % (r_stop, len(sub_stop), r_stop / len(sub_stop) * 100,
       a_stop, len(sub_stop), a_stop / len(sub_stop) * 100))
say('    → 规则在这一类多对 %d 条，占它全部领先(%d 条)的 %.0f%%'
    % (gain_stop, gain_all, gain_stop / gain_all * 100))
say()
say('  ⚠️ 但「暂停」这个标签的定义就是「可见窗口净额 < -阈值」——')
say('     而规则的止损线是「可见窗口净额 <= 0」。')
say('     答案本身就写在可见净额上，规则等于把标签的生成公式抄了一遍。')
say('     ⇒ 这 %d 条对，是同义反复，不是「规则比 AI 聪明」。' % gain_stop)

say()
say('=' * 78)
say('关键二：把「暂停」类剔掉，只比真正要推断未来的部分（加预算 / 观察）')
say('=' * 78)
say()
sub2 = [n for n in names if truth[n]['正确动作'] in ('加预算', '观察')]
a2 = sum(1 for n in sub2 if ai_act(n) == truth[n]['正确动作'])
r2 = sum(1 for n in sub2 if rule_act(n) == truth[n]['正确动作'])
r2n = sum(1 for n in sub2 if rule_no_net(n) == truth[n]['正确动作'])
say('  只看得见窗口 → 推断未来 不该更赚（这两类共 %d 条）：' % len(sub2))
say('    AI                          %3d/%d = %.1f%%' % (a2, len(sub2), a2 / len(sub2) * 100))
say('    规则（含止损线）              %3d/%d = %.1f%%' % (r2, len(sub2), r2 / len(sub2) * 100))
say('    规则（只留加钱提名线）         %3d/%d = %.1f%%   ← 完全不看净额'
    % (r2n, len(sub2), r2n / len(sub2) * 100))
say()
say('  加预算提名（规则说了多少次、对了多少次）：')
for tag, fn in (('规则（含止损线）', rule_act), ('规则（只留提名线）', rule_no_net)):
    nom = [n for n in names if fn(n) == '加预算']
    ok = sum(1 for n in nom if truth[n]['正确动作'] == '加预算')
    say('    %-18s 提名 %2d 条，其中真该加 %2d 条 → 精度 %.1f%%'
        % (tag, len(nom), ok, ok / len(nom) * 100 if nom else 0))
nom_ai = [n for n in names if ai_act(n) == '加预算']
ok_ai = sum(1 for n in nom_ai if truth[n]['正确动作'] == '加预算')
say('    %-18s 提名 %2d 条，其中真该加 %2d 条 → 精度 %.1f%%'
    % ('AI', len(nom_ai), ok_ai, ok_ai / len(nom_ai) * 100 if nom_ai else 0))

say()
say('=' * 78)
say('关键三：规则看不见的东西')
say('=' * 78)
say()
say('  规则只用 3 列：前5天净 / 顶格率5 / 频次5。')
say('  它不读：逐日曲线、漏斗各环、目标类型、预算类型、素材/受众上下文、任何文字。')
say('  它给不出理由，也发现不了异常。')
say()
say('  实证：第 6 轮 AI 报出一条「购物次数=0 却写出转化价值」的行 ——')
say('        那是数据生成器的 bug，规则永远发现不了这件事，因为它不做合理性检查。')

say()
say('=' * 78)
say('结论口径')
say('=' * 78)
say()
say('  准确的说法不是「规则比 AI 好」，而是：')
say('    1) 在「按净额算账」这个动作上，算式必然赢语言模型（而且这道题答案就是这么定义的）;')
say('    2) 在「预测放量值不值」上，规则只赢一点点，且同样靠猜;')
say('    3) 在「讲清理由 / 发现异常 / 面对新形态」上，规则是零分。')

with io.open(P_OUT, 'w', encoding='utf-8') as f:
    f.write('\n'.join(L))
