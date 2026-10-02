# -*- coding: utf-8 -*-
"""验证新落的「关停护栏」是否真的等于之前分析里那版 guard_code。

链路：v5 CSV --fb_test_loader--> rows --make_snapshot--> 快照(含 net_view)
      + 已落盘的 AI 结论（AI逐条结论-v11.csv）
      --enforce_risk_guardrails(真实引擎函数)--> 护栏后的结论
再按 _guard_cmp.py 的口径判分（钱 = 后 5 天净收益）。

要验三件事：
  A. net_view.net 是否 == 真值表的「前5天净」（口径一致，否则护栏打偏）
  B. 逐条比对：护栏后的动作 == guard_code(net, AI原动作)  → 证明实现无偏差
  C. 分数是否复现 ① 纯AI 57 / ② AI+代码护栏 68 / ④ 纯规则 80
"""
import csv
import io
import json
import os
import sys

D = os.path.dirname(os.path.abspath(__file__))
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from fb_test_loader import load_file                                      # noqa: E402
from fb_ai_engine import make_snapshot, enforce_risk_guardrails           # noqa: E402

CSV_V5 = os.path.join(D, '模拟广告数据-100广告10天-v5.csv')
P_AI = os.path.join(D, 'AI逐条结论-v11.csv')
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_OUT = os.path.join(D, '_test_guard_out.txt')

L = []


def say(s=''):
    L.append(s)


def num(x):
    try:
        return float(str(x).strip())
    except Exception:
        return 0.0


# ---------- 1. 数据与快照 ----------
rows, meta = load_file(CSV_V5)
snap = make_snapshot(rows)
by_id = {str(r.get('id')): r for r in snap}

with io.open(P_TRUTH, encoding='utf-8-sig') as f:
    truth = {r['广告']: r for r in csv.DictReader(f)}

ai = {}
with io.open(P_AI, encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        ai[r['对象']] = r

say('=' * 78)
say('关停护栏落地验证（v5 模拟数据 100 条 · 后 5 天净收益口径）')
say('=' * 78)
say()
say('快照 %d 条 / AI 结论 %d 条 / 真值 %d 条' % (len(snap), len(ai), len(truth)))
say('快照 id 样例: %s' % list(by_id.keys())[:2])
say('AI 结论键样例: %s' % list(ai.keys())[:2])
have_nv = sum(1 for r in snap if r.get('net_view'))
say('快照带 net_view 的条数: %d / %d' % (have_nv, len(snap)))
say()

# ---------- 2. 口径校验 A：net_view.net vs 真值「前5天净」 ----------
say('-' * 78)
say('A. 口径校验：net_view.net 是否等于真值表「前5天净」')
say('-' * 78)
mism = []
for name, t in truth.items():
    nv = (by_id.get(str(name)) or {}).get('net_view')
    if not nv:
        mism.append((name, None, num(t['前5天净']), '缺 net_view'))
        continue
    d = float(nv.get('net', 0)) - num(t['前5天净'])
    if abs(d) > 0.02:
        mism.append((name, nv.get('net'), num(t['前5天净']), '差 %.2f' % d))
say('  不一致条数: %d / %d' % (len(mism), len(truth)))
for m in mism[:8]:
    say('    %-46s net_view=%-10s 真值=%-10s %s' % (str(m[0])[:46], m[1], m[2], m[3]))
say()

# ---------- 3. 组装 engine 风格 suggestions，跑真实护栏 ----------
sugg = []
for key, r in ai.items():
    sugg.append({
        'campaign_id': key,
        'action': r['AI动作'],
        'budget_change_pct': 0,
        'reason': r.get('理由', ''),
    })

cfg = {}
with io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ai_config.example.json'), encoding='utf-8') as f:
    cfg = json.load(f)

guarded, gstat = enforce_risk_guardrails(sugg, snap, cfg)

say('-' * 78)
say('B. 逐条比对：护栏后动作 vs guard_code(净额, AI原动作)')
say('-' * 78)
say('  护栏 stats: %s' % gstat)
say()

STOPPED = ('pause', 'decrease_budget')


def act_raw(a):
    if a in STOPPED:
        return 'stop'
    return 'up' if a == 'increase_budget' else 'observe'


def guard_code(net, act_raw_):
    """之前分析里验证过的那版（_guard_cmp.py 第 72 行）"""
    if net <= 0:
        return 'stop'
    if act_raw_ == 'stop':
        return 'observe'
    return act_raw_


bad = []
changed = []
for s in guarded:
    name = str(s['campaign_id'])
    nv = (by_id.get(name) or {}).get('net_view') or {}
    net = nv.get('net')
    if net is None:
        continue
    exp = guard_code(float(net), act_raw(s.get('ai_action') or s['action']))
    got = act_raw(s['action'])
    if exp != got:
        bad.append((name, net, exp, got))
    if s.get('guardrail'):
        changed.append((name, s.get('ai_action'), s['action'], net, s['guardrail']))
say('  与 guard_code 不一致的条数: %d （0 = 实现无偏差）' % len(bad))
for b in bad[:10]:
    say('    %-46s 净 %+8.2f 期望 %-8s 实际 %-8s' % (str(b[0])[:46], b[1], b[2], b[3]))
say()
say('  护栏实际改动 %d 条：' % len(changed))
for c in changed:
    say('    %-44s %-16s -> %-8s 净 %+8.2f  [%s]'
        % (str(c[0])[:44], c[1], c[2], c[3], c[4]))
say()

# ---------- 4. 判分（口径同 _guard_cmp.py）----------
def pay(t, act):
    if act == 'stop':
        return 0.0
    return num(t['加预算_净']) if act == 'up' else num(t['不动_净'])


def netof(name):
    return num(truth[name]['前5天净'])


def rule_all(name):
    t = truth[name]
    if netof(name) <= 0:
        return 'stop'
    if num(t['_顶格率5']) >= 0.90 and num(t['_频次5']) < 1.35:
        return 'up'
    return 'observe'


names = [n for n in truth if n in ai and n in by_id and (by_id[n].get('net_view'))]
post = {str(s['campaign_id']): s for s in guarded}

PLANS = [
    ('① 纯 AI（现状）', lambda n: act_raw(ai[n]['AI动作'])),
    ('② AI + 代码护栏（本轮落地）', lambda n: act_raw(post[n]['action'])),
    ('④ 纯规则（不用 AI）', rule_all),
]
CN = {'stop': '暂停', 'observe': '观察', 'up': '加预算'}

say('-' * 78)
say('C. 判分（%d 条有 net_view 的广告）' % len(names))
say('-' * 78)
say('  %-26s %8s %9s %9s %11s %11s' % ('方案', '总分', '关停召回', '关停精度', '漏杀少省$', '误杀少赚$'))
say('  ' + '-' * 74)
for label, pick in PLANS:
    hit = tp = fp = fn_ = 0
    miss_money = over_money = 0.0
    total = 0.0
    for n in names:
        act = pick(n)
        total += pay(truth[n], act)
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
    rec = tp / (tp + fn_) * 100 if tp + fn_ else 0
    pre = tp / (tp + fp) * 100 if tp + fp else 0
    say('  %-26s %7.1f%% %8.0f%% %8.0f%% %11.2f %11.2f'
        % (label, hit / len(names) * 100, rec, pre, -miss_money, -over_money))
    say('  %-26s %s后 5 天合计净收益 %+.2f' % ('', ' ' * 8, total))

say()
say('  （漏杀少省$ 越接近 0 越好 = 该停的没漏；误杀少赚$ 越接近 0 越好 = 该留的没砍）')
say()

# ---------- 5. 提名样例 ----------
noms = [s for s in guarded if s.get('nomination')]
say('-' * 78)
say('D. 加预算提名（只提名、不改动作）共 %d 条，前 6 条：' % len(noms))
say('-' * 78)
for s in noms[:6]:
    nv = (by_id.get(str(s['campaign_id'])) or {}).get('net_view') or {}
    t = truth.get(str(s['campaign_id']))
    say('  %-42s AI=%-8s 净 %+8.2f 顶格 %.2f 频次 %.2f ｜ 真值=%s'
        % (str(s['campaign_id'])[:42], s['action'], nv.get('net', 0),
           nv.get('fill_rate', 0), nv.get('freq_last', 0),
           t['正确动作'] if t else '?'))
if noms:
    hitn = sum(1 for s in noms if truth.get(str(s['campaign_id']), {}).get('正确动作') == '加预算')
    say('  → 提名命中「真值=加预算」 %d / %d = %.0f%%' % (hitn, len(noms), hitn / len(noms) * 100))

with io.open(P_OUT, 'w', encoding='utf-8') as f:
    f.write('\n'.join(L))
print('\n'.join(L))
