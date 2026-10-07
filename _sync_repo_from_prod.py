# -*- coding: utf-8 -*-
"""把生产的 fb_ai_engine.py 同步到仓库版，**保留仓库独有的「学习型判断层接入」块**。

为什么不能直接 cp 覆盖（2026-09 教训）：仓库版末尾比生产多一整块 make_table_judger /
blend_with_judger，直接覆盖会把它删掉，而且下次 diff 会读成「两边一样」→ 反向覆盖 → 真删。
所以这里只做「生产全文 + 仓库独有块」的拼接，并逐项校验。
"""
import io
import os

PROD = r'D:\AdTools\fb_ai_engine.py'
REPO = r'D:\workbuddy\2026-09-29-21-17-28\fb-ads-ai-judge\fb_ai_engine.py'
MARK = '# ---------- 学习型判断层接入'

prod = io.open(PROD, encoding='utf-8').read()
repo = io.open(REPO, encoding='utf-8').read()

assert MARK not in prod, '生产里出现了仓库独有块标记 —— 方向可能反了，先人工确认'
assert repo.count(MARK) == 1, '仓库里独有块标记出现 %d 次，预期 1 次' % repo.count(MARK)

tail = repo[repo.index(MARK):]
assert 'def blend_with_judger' in tail, '独有块内容不完整，预期含 blend_with_judger'

if not prod.endswith('\n'):
    prod += '\n'
new = prod + '\n' + tail

io.open(REPO, 'w', encoding='utf-8', newline='').write(new)

# 回读校验：独有块必须逐字节保留，且生产改动全部到位
back = io.open(REPO, encoding='utf-8').read()
assert back[back.index(MARK):] == tail, '独有块被改动'
assert back.startswith(prod.rstrip('\n')), '生产内容未完整写入'
print('OK  repo_lines=%d (was %d)  tail_bytes=%d' % (len(back.splitlines()), len(repo.splitlines()), len(tail)))
# 生产标记自检：确认最新一批生产改动都随同步进了仓库。
# 2026-10-07 更新：原检查的两个串（blocked_by': 'target_cpa' / 两把尺子都要过）
# 在 10-03「CPA 降为参考线、ROI 撤出」后就不存在了，恒为 False —— 过时自检比没有更糟
# （每次跑都报红，真出问题时反而分不清）。改为当前实际存在的标记，且**硬失败**。
PROD_MARKERS = (
    'def _target_cpa(config):',              # CPA 业务目标
    "'blocked_by_precondition'",             # 前置条件护栏
    "row['visible_net'] = _nv.get('net')",   # 2026-10-07 判断时可见净
)
missing = [k for k in PROD_MARKERS if k not in back]
assert not missing, '同步后仓库缺少预期的生产标记：%s' % missing
print('keys_check: OK (%d/%d)' % (len(PROD_MARKERS), len(PROD_MARKERS)))
