# -*- coding: utf-8 -*-
"""对比两次真喂 AI 的结果，回答两个问题：

  ① 给 prompt 里加上 net_view（程序算好的钱账）之后，AI 的**原判**有没有变好？
  ② 加上关停护栏之后，最终分数是多少？

  · 旧轮 = 提示词里没有 net_view、无护栏（落盘 json 里的 suggestions 就是 AI 原判）
  · 新轮 = 提示词里有 net_view、过了护栏（用 s['ai_action'] 还原 AI 原判）

判分口径与 _guard_cmp.py 一致：钱 = Σ 后 5 天净收益。
"""
import csv
import io
import json
import os
import sys

D = os.path.dirname(os.path.abspath(__file__))
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_OUT = os.path.join(D, '_v12_compare_out.txt')
OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_out')

# ⚠️ 基线要挑对：143346 那轮**带提示词护栏**（63%），拿它当「纯 AI」会得出错误的 +2 个百分点。
#    132814 才是无护栏、旧 prompt 的纯 AI 基线（57%）。
OLD = os.path.join(OUTDIR, '测试结果-20261002-132814.json')

L = []


def say(s=''):
    L.append(s)


def num(x):
    try:
        return float(str(x).strip())
    except Exception:
        return 0.0


# 最新落盘 = 新轮（按修改时间）
#
# ⚠️ `_out/` 是「真喂模型」的落盘产物，属于本机实验记录，**不随仓库发布**。
#    公开仓库里没有它 —— 所以这里必须优雅跳过并讲清原因，
#    否则别人 clone 下来第一件事就是 FileNotFoundError，会以为仓库坏了。
_HINT = ('跳过：本脚本对比的是两轮「真喂 AI」的落盘结果，需要 _out/ 目录'
         '（本机实验产物，不随仓库发布）。要直接看结论，读 docs/evidence/_v12_compare_out.txt。')
if not os.path.isdir(OUTDIR):
    print(_HINT)
    sys.exit(0)
cands = [os.path.join(OUTDIR, f) for f in os.listdir(OUTDIR)
         if f.startswith('测试结果-') and f.endswith('.json')]
cands.sort(key=lambda p: os.path.getmtime(p))
if not cands:
    print(_HINT.replace('需要 _out/ 目录', '_out/ 里没有 测试结果-*.json'))
    sys.exit(0)
if not os.path.exists(OLD):
    print('跳过：找不到基线 %s（无 net_view 那一轮的落盘）。'
          '要直接看结论，读 docs/evidence/_v12_compare_out.txt。' % os.path.basename(OLD))
    sys.exit(0)
NEW = cands[-1]

say('=' * 78)
say('改 prompt 前后对照（同数据、同模型、同链路）')
say('=' * 78)
say('旧轮 json: %s' % os.path.basename(OLD))
say('新轮 json: %s' % os.path.basename(NEW))
say()

with io.open(P_TRUTH, encoding='utf-8-sig') as f:
    truth = {r['广告']: r for r in csv.DictReader(f)}

STOPPED = ('pause', 'decrease_budget')


def act_raw(a):
    if a in STOPPED:
        return 'stop'
    return 'up' if a == 'increase_budget' else 'observe'


def load(fn):
    with io.open(fn, encoding='utf-8') as f:
        return json.load(f)['suggestions']


def pay(t, act):
    if act == 'stop':
        return 0.0
    return num(t['加预算_净']) if act == 'up' else num(t['不动_净'])


def score(rows, pick, label):
    """rows: {name: suggestion}；pick: name -> 'stop'|'observe'|'up'"""
    names = [n for n in truth if n in rows]
    hit = tp = fp = fn_ = 0
    miss = over = 0.0
    total = 0.0
    up_hit = up_tot = 0
    for n in names:
        act = pick(n)
        total += pay(truth[n], act)
        CN2 = {'stop': '暂停', 'observe': '观察', 'up': '加预算'}
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
    rec = tp / (tp + fn_) * 100 if tp + fn_ else 0
    pre = tp / (tp + fp) * 100 if tp + fp else 0
    say('  %-30s %7.1f%%  关停召回 %5.0f%%  关停精度 %5.0f%%  加预算召回 %5.1f%%  漏杀 %8.2f  误杀 %8.2f  总净 %+10.2f'
        % (label, hit / len(names) * 100, rec, pre,
           up_hit / up_tot * 100 if up_tot else 0, -miss, -over, total))
    return hit / len(names) * 100


old = {str(s['campaign_id']): s for s in load(OLD)}
new = {str(s['campaign_id']): s for s in load(NEW)}

say('条数: 旧轮 %d / 新轮 %d' % (len(old), len(new)))
say()
say('  %s' % ('-' * 150))
t_old = score(old, lambda n: act_raw(old[n]['action']), '① 旧轮 AI 原判（无 net_view）')
t_new = score(new, lambda n: act_raw(new[n].get('ai_action') or new[n]['action']),
              '② 新轮 AI 原判（有 net_view）')
t_g = score(new, lambda n: act_raw(new[n]['action']), '③ 新轮 AI + 关停护栏（生产形态）')
say('  %s' % ('-' * 150))
say()
say('  AI 原判变化: %+.1f 个百分点（%s）'
    % (t_new - t_old, 'net_view 让 AI 变好' if t_new > t_old else
       ('没变' if abs(t_new - t_old) < 0.01 else 'net_view 没帮上 AI')))
say('  护栏贡献:   %+.1f 个百分点' % (t_g - t_new))
say()

# 逐条看护栏改了什么
chg = [(n, s) for n, s in new.items() if s.get('guardrail')]
say('  护栏改动 %d 条：' % len(chg))
for n, s in chg:
    say('    %-44s %-16s -> %-8s  [%s]'
        % (str(n)[:44], s.get('ai_action'), s['action'], s.get('guardrail')))
noms = [(n, s) for n, s in new.items() if s.get('nomination')]
say()
say('  加预算提名 %d 条：' % len(noms))
for n, s in noms[:15]:
    t = truth.get(n)
    say('    %-42s AI=%-8s ｜ 真值=%s' % (str(n)[:42], s['action'], t['正确动作'] if t else '?'))
if noms:
    h = sum(1 for n, _s in noms if truth.get(n, {}).get('正确动作') == '加预算')
    say('    → 命中真值「加预算」 %d / %d = %.0f%%' % (h, len(noms), h / len(noms) * 100))

say()
say('  本轮日志（新轮）：')
with io.open(NEW, encoding='utf-8') as f:
    _d = json.load(f)
say('    timing: %s' % json.dumps(_d.get('timing', {}), ensure_ascii=False)[:400])

with io.open(P_OUT, 'w', encoding='utf-8') as f:
    f.write('\n'.join(L))
print('\n'.join(L))
