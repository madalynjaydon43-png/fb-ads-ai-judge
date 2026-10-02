# -*- coding: utf-8 -*-
"""把所有落盘的测试结果按时间排一遍，对 v5 真值判分。

目的：**先确认基准是哪一个**，再谈「改 prompt 有没有用」。
上一版脚本直接拿 143346 当基线，结果那一轮其实是「带提示词护栏」的，
如果照着比就会得出错误的 +2 个百分点。（这正是本项目反复踩的「口径」坑。）
"""
import csv
import io
import json
import os

D = os.path.dirname(os.path.abspath(__file__))
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_OUT = os.path.join(D, '_v12_history_out.txt')
OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_out')

L = []


def say(s=''):
    L.append(s)


def num(x):
    try:
        return float(str(x).strip())
    except Exception:
        return 0.0


with io.open(P_TRUTH, encoding='utf-8-sig') as f:
    truth = {r['广告']: r for r in csv.DictReader(f)}
STOPPED = ('pause', 'decrease_budget')


def act_raw(a):
    if a in STOPPED:
        return 'stop'
    return 'up' if a == 'increase_budget' else 'observe'


def pay(t, act):
    if act == 'stop':
        return 0.0
    return num(t['加预算_净']) if act == 'up' else num(t['不动_净'])


CN2 = {'stop': '暂停', 'observe': '观察', 'up': '加预算'}

rows = []
for fn in os.listdir(OUTDIR):
    if not (fn.startswith('测试结果-') and fn.endswith('.json')):
        continue
    p = os.path.join(OUTDIR, fn)
    try:
        with io.open(p, encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        continue
    meta = d.get('meta') or {}
    fname = str(meta.get('file') or '')
    sg = d.get('suggestions') or []
    guarded = sum(1 for s in sg if s.get('guardrail'))
    n_sub = sum(1 for s in sg if s.get('nomination'))
    # 有 ai_action 字段 = 过了护栏的轮次
    rows.append((os.path.getmtime(p), fn, fname, sg, guarded, n_sub, d.get('timing') or {}))

rows.sort()

say('=' * 132)
say('落盘历史 × v5 真值 判分（AI 原判口径：用 ai_action 还原，没有则用 action）')
say('=' * 132)
say()
say('  %-24s %-30s %4s %5s %5s %8s %8s %5s %5s %8s %10s' %
    ('落盘文件', '数据文件', '条数', '总分', '关停召回', '关停精度', '加预算召回', '漏杀$', '误杀$', '护栏改动', '总净'))
say('  ' + '-' * 128)

hits = []
for mt, fn, fname, sg, guarded, n_sub, T in rows:
    got = {str(s['campaign_id']): s for s in sg}
    names = [n for n in truth if n in got]
    if not names:
        continue
    hit = tp = fp = fn_ = up_hit = up_tot = 0
    miss = over = 0.0
    total = 0.0
    for n in names:
        s = got[n]
        act = act_raw(s.get('ai_action') or s['action'])
        total += pay(truth[n], act)
        if CN2[act] == truth[n]['正确动作']:
            hit += 1
        if truth[n]['正确动作'] == '加预算':
            up_tot += 1
            if act == 'up':
                up_hit += 1
        if act == 'stop' and truth[n]['正确动作'] == '暂停':
            tp += 1
        elif act == 'stop':
            fp += 1
            over += pay(truth[n], 'observe') - pay(truth[n], 'stop')
        elif truth[n]['正确动作'] == '暂停':
            fn_ += 1
            miss += -pay(truth[n], 'observe')
    acc = hit / len(names) * 100
    rec = tp / (tp + fn_) * 100 if tp + fn_ else 0
    pre = tp / (tp + fp) * 100 if tp + fp else 0
    upr = up_hit / up_tot * 100 if up_tot else 0
    data_tag = 'v5' if 'v5' in fname else ('v4' if 'v4' in fname else fname[-28:])
    say('  %-24s %-30s %4d %5.1f%% %6.0f%% %7.0f%% %8.1f%% %7.2f %7.2f %8d %10.2f'
        % (fn.replace('测试结果-', '').replace('.json', ''), data_tag, len(names),
           acc, rec, pre, upr, -miss, -over, guarded, total))
    hits.append((fn, data_tag, acc, rec, pre, upr, -miss, -over, guarded, n_sub, total))

say()
say('  说明：')
say('    · 「护栏改动」= 该轮里带 guardrail 字段的条数（0 = 那轮没护栏）')
say('    · 只对 v5 数据轮次有意义 —— v4 数据轮的条目标签不同，比分不可比')
say('    · ai_action 字段只在护栏真改了动作时才写，所以「有护栏」不影响这里的 AI 原判口径')
say()

# 挑出 v5 轮次，标出「无护栏」与「有护栏且改了 3 条」这两类
v5 = [h for h in hits if h[1] == 'v5']
say('  v5 轮次按时间：')
for h in v5:
    say('    %s  总分 %.1f%%  护栏改动 %d  提名 %d  总净 %+.2f'
        % (h[0].replace('测试结果-', '').replace('.json', ''), h[2], h[8], h[9], h[10]))

with io.open(P_OUT, 'w', encoding='utf-8') as f:
    f.write('\n'.join(L))
print('\n'.join(L))
