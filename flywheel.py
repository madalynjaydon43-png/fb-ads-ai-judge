# -*- coding: utf-8 -*-
"""flywheel.py —— 「判断时快照 → 后来结局 → 标签」的飞轮落盘层。

一次判断本身没有价值，**判断 + 后来的结局**才是训练数据。
所以每次判断都把「当时的证据」原样存一份；等业务跑出结局再回填标签。

四步飞轮
========
    ① 判断    record_batch()        每次判断 → 快照落 learning_store.jsonl（未标注）
    ② 结局    业务自然产生          广告后来的花费/回收，或模拟数据的反事实两版
    ③ 标签    backfill_*()          derive_label() 把结局翻译成 pause/加/观察
    ④ 重训    table_judger.fit()    标签够了模型上岗，不够就继续回退规则
             再由 learn_cycle.py 判卷，确认没退步才换上去

存储格式（每行一条，JSON Lines）
================================
    {"ts": "...", "ad_id": "...", "ad_name": "...", "features": {...30 维...},
     "tool_action": "observe|null", "outcome": {...}|null}

outcome:
    {"window": ["2026-09-28","2026-10-02"], "spend": 87.3, "purchase_value": 210.5,
     "fill_rate": 0.8, "net": 123.2, "visible_net": 96.4, "label": "increase_budget",
     "net_stop": 0.0, "net_hold": 123.2, "net_up": 131.4,
     "source": "simulated"|"real", "counterfactual": true|false}

🔴 数据来源假设（不要假装有反事实真值）
  · `source="simulated"`：数据由 `gen_v5.py` 反事实式生成（同一条广告跑两遍定答案），
    所以 net_stop / net_hold / net_up 三版**都真实存在**，counterfactual=true。
  · `source="real"`：Ads Manager 导出的真实轨迹**只有一条**，不存在「如果不加预算会怎样」。
    所以只能令 net_hold = net_up = 真实净，counterfactual=false ——
    用它训练出来的模型只知道「真实发生的世界」，不知道反事实。
    这不是缺陷，是必须写在脸上的一条局限。

只依赖标准库（json/csv/io/os），不依赖 sklearn —— 存数据不需要模型库。
"""
import csv
import io
import json
import os
from datetime import datetime

from table_judger import (
    FEATURE_KEYS,
    days_from_csv_rows,
    derive_label,
    extract_features,
    fnum,
    read_grouped_rows,
    _features_from_days,
)

D = os.path.dirname(os.path.abspath(__file__))
P_STORE = os.path.join(D, 'learning_store.jsonl')


# ==================== ① 判断时落盘 ====================

def _snapshot_day(snap):
    """取「这条快照代表哪一天」——用快照最后一天的日期，比用 today 稳。"""
    daily = (snap or {}).get('daily_spend') or []
    if daily:
        return str(daily[-1].get('date') or '')
    return ''


def _snapshot_visible_net(snap, feat=None):
    """取「判断时可见净」。优先快照顶层 visible_net（make_snapshot 显式给的），
    其次 net_view.net，最后用特征重算（收入 − 花费）。取不到返回 None。

    这是 derive_label v2 的暂停口径（可见净），与「后续窗口净 outcome.net」是两回事。
    """
    snap = snap or {}
    v = snap.get('visible_net')
    if v is None:
        v = (snap.get('net_view') or {}).get('net')
    if v is None and feat and '收入' in feat and '花费' in feat:
        v = float(feat['收入']) - float(feat['花费'])
    if v is None:
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def record_snapshot(snap, tool_action=None, path=None, ts=None):
    """把一条判断快照追加进 store。同广告同天**至多一条**（重复调用返回 False）。

    返回 True = 新写入；False = 已存在（防刷屏）。
    """
    path = path or P_STORE
    ad_id = str((snap or {}).get('id') or '')
    if not ad_id:
        return False
    day = _snapshot_day(snap) or datetime.now().strftime('%Y-%m-%d')
    key = '%s|%s' % (ad_id, day)
    if key in _existing_keys(path):
        return False
    feat, _ = extract_features(snapshot=snap)
    if feat is None:
        return False
    rec = {
        'ts': ts or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'ad_id': ad_id,
        'ad_name': str((snap or {}).get('name') or ''),
        'key': key,
        # 判断时可见净（落盘存档）：打标时优先用它（derive_label v2 的暂停口径）。
        'visible_net': _snapshot_visible_net(snap, feat),
        'features': feat,
        'tool_action': tool_action,
        'outcome': None,
    }
    _append(rec, path)
    return True


def record_batch(snapshots, suggestions=None, path=None):
    """一次判断后批量落盘。suggestions 为同格式建议列表（取 action 存 tool_action）。"""
    act_by_id = {}
    for s in (suggestions or []):
        act_by_id[str(s.get('campaign_id'))] = s.get('action')
    n = 0
    for snap in (snapshots or []):
        if record_snapshot(snap, act_by_id.get(str((snap or {}).get('id'))), path=path):
            n += 1
    return n


def _existing_keys(path):
    keys = set()
    if not os.path.exists(path):
        return keys
    with io.open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get('key'):
                keys.add(rec['key'])
    return keys


def _append(rec, path):
    with io.open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


# ==================== ② / ③ 回填标签 ====================

def _rewrite_with_outcomes(new_records, path):
    """把回填结果合并进 store：同 key 覆盖 outcome，新 key 追加。返回 (新增, 更新)。"""
    path = path or P_STORE
    by_key = {}
    order = []
    if os.path.exists(path):
        with io.open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                k = rec.get('key') or ('%s|%s' % (rec.get('ad_id'), rec.get('ts')))
                rec.setdefault('key', k)
                if k not in by_key:
                    order.append(k)
                by_key[k] = rec
    added = updated = 0
    for rec in new_records:
        k = rec['key']
        if k in by_key:
            by_key[k]['outcome'] = rec['outcome']
            updated += 1
        else:
            by_key[k] = rec
            order.append(k)
            added += 1
    tmp = path + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8') as f:
        for k in order:
            f.write(json.dumps(by_key[k], ensure_ascii=False) + '\n')
    os.replace(tmp, path)
    return added, updated


def build_records_from_truth(data_csv=None, truth_csv=None, hid_csv=None):
    """构造「模拟模式」带标签记录（不落盘），供回填与判卷共用。

    数据来源假设（三份文件的角色，2026-10-02 实测确认）：
      · `模拟广告数据-100广告10天-v5.csv` = **判断时可见窗口**（100 广告 × 5 天，
        中文列，和真喂 AI 的是同一份）。→ 算 30 维特征。
      · `真值表-逐日-v5.csv` = **后续窗口的反事实逐日**（100 广告 × 2 方案 × 5 天，
        「方案」∈ {不动, 加预算}）。→ 算 outcome 的花费/收入/净/平均花费率。
      · `真值表-v5.csv` = 后续窗口两版的**汇总**（不动_净 / 加预算_净）+ 隐藏机制列。
        → 提供反事实净额，供判卷算钱。

    对账关系（已在测试里断言）：逐日「方案=不动」的期望净合计 == 真值表 `不动_净`；
    前 5 天花费合计 == 真值表 `总花费`。

    由此得 counterfactual=True：「如果不加预算会怎样」在模拟数据里**真的有观测**。
    """
    data_csv = data_csv or os.path.join(D, '模拟广告数据-100广告10天-v5.csv')
    truth_csv = truth_csv or os.path.join(D, '真值表-v5.csv')
    hid_csv = hid_csv or os.path.join(D, '真值表-逐日-v5.csv')

    with io.open(truth_csv, encoding='utf-8-sig') as f:
        truth = {r['广告']: r for r in csv.DictReader(f)}

    hid = {}
    with io.open(hid_csv, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            hid.setdefault(r['广告'], {}).setdefault(r['方案'], []).append(r)

    # 列名走别名解析（不再只认 Ads Manager 长列名）；缺列不静默 ——
    # 拿不到必需列会在这里告警/报错，而不是把 0 当成真值。
    # 别名表见 table_judger.COL_ALIASES。
    g, meta = read_grouped_rows(data_csv, strict=False, quiet=True)
    dc = meta['date_col']
    bcol = meta['colmap'].get('budget')
    _ = bcol
    icol = meta['id_col']
    recs = []
    for name, rows in g.items():
        rows.sort(key=lambda z: str(z.get(dc, '') if dc else ''))
        feat = _features_from_days(days_from_csv_rows(rows, meta['colmap']))
        if feat is None:
            continue
        nb = (hid.get(name) or {}).get('不动') or []
        if not nb:
            continue
        nb.sort(key=lambda z: int(r_int(z.get('第几天'))))
        budget = fnum(nb[0].get('预算'))
        spend = sum(fnum(r['花费']) for r in nb)
        rev = sum(fnum(r['期望收入']) for r in nb)
        net = sum(fnum(r['期望净']) for r in nb)
        # 「顶格率」= 平均花费率（花费 /(预算×天数)）—— 与 _solvability_v5 的「花费率」
        # 特征、真值表 `_顶格率5` 同口径。注意它**不等于** fb_ai_engine.net_view.fill_rate
        # （那个是「花费 ≥95% 预算的天数占比」），两把尺子，别混用（详见 table_judger 文件头）。
        fill = (spend / (budget * len(nb))) if budget else 0.0
        t = truth.get(name, {})
        outcome = {
            'window': _next_days(rows[-1].get(dc, '') if dc else '', len(nb)),
            'spend': round(spend, 2),
            'purchase_value': round(rev, 2),
            'fill_rate': round(fill, 3),
            'net': round(net, 2),
            # 判断时可见净（前 5 天）—— 暂停标签的口径（见 derive_label v2）。
            # 与『后续净 net』是两个窗口，别混。
            'visible_net': round(feat['收入'] - feat['花费'], 2),
            'net_stop': 0.0,
            'net_hold': round(fnum(t.get('不动_净')), 2),
            'net_up': round(fnum(t.get('加预算_净')), 2),
            'source': 'simulated',
            'counterfactual': True,
        }
        outcome['label'] = derive_label(outcome)
        ad_id = str((rows[0].get(icol) if icol else None) or name)
        recs.append({
            'ts': 'backfill',
            'ad_id': ad_id,
            'ad_name': name,
            'key': '%s|%s' % (ad_id, outcome['window'][-1]),
            'features': feat,
            'tool_action': None,
            'outcome': outcome,
        })
    return recs


def r_int(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def _next_days(last_date, n):
    """从可见窗口最后一天往后推 n 天（纯展示用，缺日期时返回空串）。"""
    if not last_date:
        return ['', '']
    try:
        from datetime import timedelta
        d0 = datetime.strptime(str(last_date), '%Y-%m-%d')
        return [(d0 + timedelta(days=1)).strftime('%Y-%m-%d'),
                (d0 + timedelta(days=n)).strftime('%Y-%m-%d')]
    except ValueError:
        return ['', '']



def backfill_from_truth(data_csv=None, truth_csv=None, path=None, hid_csv=None):
    """模拟模式回填（落盘版）：构造记录并写进 store。返回 (新增, 更新)。"""
    return _rewrite_with_outcomes(
        build_records_from_truth(data_csv, truth_csv, hid_csv), path or P_STORE)


def build_records_from_window(csv_path, head_days=5, split_date=None):
    """真实模式回填：从 Ads Manager 真实导出 CSV 打标签。

    数据来源假设（必须写清、别装作有反事实）：
      真实轨迹只有**一条**。不存在「如果当初加预算会怎样」的观测。
      所以 net_hold = net_up = 真实后续净额，counterfactual=False。
      这样训出来的模型只见过「真实发生过的世界」——
      它能学「什么样的前半段后来亏了/赚了」，学不到「加预算能不能救回来」。

    split_date: 判断时点。给了就按日期切；不给就按每条广告的前 head_days 天切。

    列名走别名解析（`table_judger.COL_ALIASES`），所以 Ads Manager 长列名、
    本仓库 pull_real3.py 拍平的短名都能吃。缺必需列会直接抛 `ColumnError` ——
    宁可跑不动，也不往 store 里写「缺列被当成 0」的假标签。
    """
    g, meta = read_grouped_rows(csv_path, strict=True)
    dc = meta['date_col']
    bcol = meta['colmap'].get('budget')
    icol = meta['id_col']

    def _d(r):
        return str(r.get(dc, '') if dc else '')

    recs = []
    for name, rows in g.items():
        rows.sort(key=lambda z: _d(z))
        if split_date:
            head_rows = [r for r in rows if _d(r) <= split_date]
            tail_rows = [r for r in rows if _d(r) > split_date]
        else:
            head_rows, tail_rows = rows[:head_days], rows[head_days:]
        if len(head_rows) < 2 or not tail_rows:
            continue
        feat = _features_from_days(days_from_csv_rows(head_rows, meta['colmap']))
        if feat is None:
            continue
        nv = _window_net_view(days_from_csv_rows(tail_rows, meta['colmap']),
                              rows[0].get(bcol) if bcol else None)
        outcome = {
            'window': [_d(tail_rows[0]), _d(tail_rows[-1])],
            'spend': round(nv['spend'], 2),
            'purchase_value': round(nv['revenue'], 2),
            'fill_rate': round(nv['fill_rate'], 3),
            'net': round(nv['net'], 2),
            # 判断时可见净（前 head_days 天）—— 暂停标签的口径（derive_label v2）
            'visible_net': round(feat['收入'] - feat['花费'], 2),
            'net_stop': 0.0,
            'net_hold': round(nv['net'], 2),
            'net_up': round(nv['net'], 2),
            'source': 'real',
            'counterfactual': False,
            'head_days': len(head_rows),
        }
        outcome['label'] = derive_label(outcome)
        ad_id = str((rows[0].get(icol) if icol else None) or name)
        recs.append({
            'ts': 'backfill',
            'ad_id': ad_id,
            'ad_name': name,
            'key': '%s|%s' % (ad_id, _d(tail_rows[-1])),
            'features': feat,
            'tool_action': None,
            'outcome': outcome,
        })
    return recs


def backfill_from_window(csv_path, path=None, head_days=5, split_date=None):
    """真实模式回填（落盘版）：构造记录并写进 store。返回 (新增, 更新)。"""
    return _rewrite_with_outcomes(
        build_records_from_window(csv_path, head_days, split_date), path or P_STORE)


def records_to_samples(recs):
    """记录列表 → 训练/判卷样本列表。标签缺失时按 derive_label 现算。

    outcome 里没有 visible_net 时，用记录级的 visible_net 补上 —— 这样
    「判断时落盘、后来补结局」的真实飞轮记录也能按可见净打标（derive_label v2）。
    """
    out = []
    for r in recs:
        feats = r.get('features')
        if not feats:
            continue
        o = dict(r.get('outcome') or {})
        if o.get('visible_net') is None and r.get('visible_net') is not None:
            o['visible_net'] = r['visible_net']
        lab = o.get('label')
        if lab is None and o:
            lab = derive_label(o)
        if lab:
            out.append({'features': feats, 'label': lab, 'outcome': o,
                        'ad_name': r.get('ad_name')})
    return out


def _window_net_view(days, budget=None):
    """后续窗口的钱账（口径与 fb_ai_engine._net_view 对齐，这里独立实现避免循环依赖）。

    注意 fill_rate 的口径：这里是**平均花费率** = 花费 /(预算×天数)，
    与真值表 `_顶格率5`、_solvability_v5 的「花费率」特征同源。
    它**不等于** fb_ai_engine.net_view.fill_rate（「花费≥95%预算的天数占比」）。
    两把尺子用途不同：标签口径统一用平均花费率（更稳、少受单日布尔噪声影响）。
    """
    days = list(days or [])
    spend = sum(float(d.get('spend') or 0) for d in days)
    rev = sum(float(d.get('purchase_value') or 0) for d in days)
    active = sum(1 for d in days if float(d.get('spend') or 0) > 0)
    bud = float(budget or 0)
    return {
        'spend': spend,
        'revenue': rev,
        'net': rev - spend,
        'fill_rate': (spend / (bud * len(days))) if (bud > 0 and days) else 0.0,
        'active_days': active,
    }


# ==================== 读取 / 统计 ====================

def load_store(path=None):
    path = path or P_STORE
    out = []
    if not os.path.exists(path):
        return out
    with io.open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def label_distribution(path=None):
    dist = {}
    for rec in load_store(path):
        o = rec.get('outcome') or {}
        lab = o.get('label')
        if lab is None and o:
            lab = derive_label(o)
        if lab:
            dist[lab] = dist.get(lab, 0) + 1
    return dist


def stats(path=None, min_labels=80):
    path = path or P_STORE
    recs = load_store(path)
    dist = label_distribution(path)
    labeled = sum(dist.values())
    return {
        'path': path,
        'snapshots': len(recs),
        'labeled': labeled,
        'unlabeled': len(recs) - labeled,
        'labels': dist,
        'min_labels': min_labels,
        'shortfall': max(0, min_labels - labeled),
        'ready': labeled >= min_labels,
    }
