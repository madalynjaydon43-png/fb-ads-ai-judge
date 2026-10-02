# -*- coding: utf-8 -*-
"""关停护栏的边界用例（不依赖 v5 数据，纯构造）。

覆盖：空输入 / 没有 net_view / 净额正好 0 / 净>0 判停 / 提名默认不改动作 /
      提名开关打开后改动作 / 已判停且净≤0 不重复改 / 无频次不提名。
断言的是**可观察副作用**（action、guardrail 字段、stats），不是函数返回值形状。
"""
import io
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from fb_ai_engine import enforce_risk_guardrails   # noqa: E402

OK = []
BAD = []


def check(name, cond, detail=''):
    (OK if cond else BAD).append('%s %s' % (name, detail))


def snap(cid, net, **kw):
    nv = {'net': net, 'days': kw.pop('days', 5)}
    for k in ('fill_rate', 'freq_last', 'freq_last_missing'):
        if k in kw:
            nv[k] = kw.pop(k)
    nv.update(kw)
    return {'id': cid, 'net_view': nv}


def sug(cid, act):
    return {'campaign_id': cid, 'action': act, 'budget_change_pct': 0, 'reason': 'r'}


# 1. 空输入
out, st = enforce_risk_guardrails([], [], {})
check('空输入', out == [] and st['forced_pause'] == 0)

# 2. 没有 net_view → 不动
s = [sug('a', 'observe')]
out, st = enforce_risk_guardrails(s, [{'id': 'a'}], {})
check('无 net_view 不动', out[0]['action'] == 'observe' and st['no_net'] == 1,
      '-> %s no_net=%s' % (out[0]['action'], st['no_net']))

# 3. 净额正好 0（边界）→ 强制停
out, st = enforce_risk_guardrails([sug('a', 'observe')], [snap('a', 0.0)], {})
check('净=0 强制停', out[0]['action'] == 'pause' and st['forced_pause'] == 1,
      '-> %s' % out[0]['action'])

# 4. 净<0 且 AI 说加预算 → 强制停，且 budget_change_pct 归零
s = [sug('a', 'increase_budget')]
s[0]['budget_change_pct'] = 20
out, st = enforce_risk_guardrails(s, [snap('a', -31.43)], {})
check('净<0 覆写加预算', out[0]['action'] == 'pause' and out[0]['budget_change_pct'] == 0
      and out[0].get('ai_action') == 'increase_budget',
      '-> %s pct=%s' % (out[0]['action'], out[0]['budget_change_pct']))

# 5. 净>0 且 AI 判停 → 降级观察
out, st = enforce_risk_guardrails([sug('a', 'pause')], [snap('a', 29.47)], {})
check('净>0 禁止杀', out[0]['action'] == 'observe' and st['blocked_kill'] == 1,
      '-> %s' % out[0]['action'])

# 6. 净>0 且 decrease_budget 也降级
out, st = enforce_risk_guardrails([sug('a', 'decrease_budget')], [snap('a', 11.23)], {})
check('净>0 禁止减预算', out[0]['action'] == 'observe', '-> %s' % out[0]['action'])

# 7. 已判停 且 净≤0 → 不改、不计数
out, st = enforce_risk_guardrails([sug('a', 'pause')], [snap('a', -5.0)], {})
check('已停不重复改', out[0]['action'] == 'pause' and st['forced_pause'] == 0, '-> %s' % out[0]['action'])

# 8. 提名：默认只挂字段、不改动作
s = [sug('a', 'observe')]
out, st = enforce_risk_guardrails(s, [snap('a', 197.02, fill_rate=1.0, freq_last=1.08)], {})
check('提名默认不改动作',
      out[0]['action'] == 'observe' and out[0].get('nomination', {}).get('suggested_action') == 'increase_budget'
      and st['nominated'] == 1,
      '-> %s nom=%s' % (out[0]['action'], bool(out[0].get('nomination'))))

# 9. 提名开关打开 → 直接改动作 + 幅度
out, st = enforce_risk_guardrails([sug('a', 'observe')],
                                  [snap('a', 197.02, fill_rate=1.0, freq_last=1.08)],
                                  {'nominate_apply': True, 'max_budget_change_pct': 20})
check('提名开关生效', out[0]['action'] == 'increase_budget' and out[0]['budget_change_pct'] == 20,
      '-> %s pct=%s' % (out[0]['action'], out[0]['budget_change_pct']))

# 10. 频次过高 → 不提名
out, st = enforce_risk_guardrails([sug('a', 'observe')],
                                  [snap('a', 197.02, fill_rate=1.0, freq_last=2.10)], {})
check('频次高不提名', not out[0].get('nomination') and st['nominated'] == 0)

# 11. 顶格率不够 → 不提名
out, st = enforce_risk_guardrails([sug('a', 'observe')],
                                  [snap('a', 197.02, fill_rate=0.40, freq_last=1.08)], {})
check('顶格低不提名', not out[0].get('nomination'))

# 12. 天数不足 → 不提名（少样本高 ROAS 是噪声）
out, st = enforce_risk_guardrails([sug('a', 'observe')],
                                  [snap('a', 197.02, fill_rate=1.0, freq_last=1.08, days=1)], {})
check('样本不足不提名', not out[0].get('nomination'))

# 13. 缺频次 → 不提名（不硬凑）
out, st = enforce_risk_guardrails([sug('a', 'observe')], [snap('a', 197.02, fill_rate=1.0)], {})
check('缺频次不提名', not out[0].get('nomination'))

# 14. AI 已经说要加 → 不重复提名
out, st = enforce_risk_guardrails([sug('a', 'increase_budget')],
                                  [snap('a', 197.02, fill_rate=1.0, freq_last=1.08)], {})
check('已加不重复提名', not out[0].get('nomination') and st['nominated'] == 0)

# 15. 理由里必须留下「AI 原判」，方便审计
out, st = enforce_risk_guardrails([sug('a', 'observe')], [snap('a', -31.43)], {})
check('理由留痕', 'AI 原判' in out[0]['reason'] and out[0]['reason'].startswith('【护栏·止损】'),
      '-> %s' % out[0]['reason'][:40])

L = ['=' * 70, '关停护栏边界用例', '=' * 70, '']
L.append('通过 %d / %d' % (len(OK), len(OK) + len(BAD)))
L.append('')
for x in OK:
    L.append('  [OK]   ' + x)
for x in BAD:
    L.append('  [FAIL] ' + x)
if BAD:
    L.append('')
    L.append('>>> 有 %d 项失败' % len(BAD))
txt = '\n'.join(L)
with io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '_test_guard_edge_out.txt'),
             'w', encoding='utf-8') as f:
    f.write(txt)
print(txt)
