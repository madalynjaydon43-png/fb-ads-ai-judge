# -*- coding: utf-8 -*-
"""table_judger.py —— 会学习的判断层（表格模型）。

定位
====
给仓库现有的「LLM 零样本判断」补一条**能从历史结局里学**的判断通道。
它不替代任何东西，只多一条路：

    代码护栏（enforce_risk_guardrails）   ← 永远压在一切模型之上
        ↑
    表格模型（本文件）                     ← 从带标签历史里学判断边界
        ↑
    LLM（现有链路）                        ← 写理由、挑矛盾、对提名做反方复核
        ↑
    规则（rule_action，本文件）             ← 冷启动回退 + 提名基线
        ↑
    人                                     ← 批准一切花钱动作

三条硬边界（与仓库既有约定一致，不要绕过）：
  1. 本模块**不执行任何广告操作**，只产出建议；
  2. 「加预算」只能产出**提名**（nomination 字段 + budget_change_pct=0），
     绝不替人拍板花钱 —— 沿用仓库现有提名制；
  3. 输出建议**必须**再过一道 `fb_ai_engine.enforce_risk_guardrails`，
     本模块自己也会调用它（见 `TableJudger.judge` 的 `apply_guardrail`），
     但调度层不要省那一遍。

口径选择（任务书允许「逐日行结构」与「快照结构」二选一）
========================================================
30 维可见特征的口径定义在 `_solvability_v5.py`，输入是 Ads Manager 导出的
**逐日中文列**；生产链路的输入是 `fb_ai_engine.make_snapshot` 的**快照**（英文键）。
如果两边各写一份特征代码，迟早漂移，而且「线上判断」和「离线判卷」就不再是同一把尺子。

所以本文件只留**一份**特征实现 `_features_from_days()`，配两个适配器把两种输入
归一化成同一种内部逐日结构：

    days_from_csv_rows(rows)   ← 中文列逐日行（与 _solvability_v5.py 完全同源）
    days_from_snapshot(snap)   ← make_snapshot 的产物

这样生产判断与离线判卷用的是同一把尺子。

标签口径（v1 · 可观测版）
========================
`derive_label(outcome)`：
    后续窗口花费 < $5          → None（样本太小，学不到东西，不参与训练）
    后续窗口净 ≤ 0             → pause
    净 > 0 且顶格率 ≥ 0.90     → increase_budget
    其余                       → observe

⚠️ 这里用的是「可见净」（回收 − 花费），**没有扣商品成本**。真实业务里净应扣货成本，
   所以这是**归因口径**不是利润口径；换成真实数据时必须先想清楚成本怎么进模型。

可选依赖：scikit-learn（只在本模块被 import 时要求；`requirements.txt` 已列）。
无 sklearn 时本模块仍可 import，但 `TableJudger.fit/predict` 会明确报错，
不会静默降级成「假装学过了」。
"""
import csv
import io
import json
import os

D = os.path.dirname(os.path.abspath(__file__))
P_STORE = os.path.join(D, 'learning_store.jsonl')

# ---------- 阈值（与 fb_ai_engine 的护栏线保持同源；不要各自为政）----------
KILL_STOP_NET = 0.0        # 可见净 ≤ 此值 → 该停
FILL_RATE_UP = 0.90        # 顶格率 ≥ 此值 → 预算卡住了它
FREQ_MAX_UP = 1.35         # 末段频次 < 此值 → 受众没看腻
MIN_LABELS = 80            # 标签下限：不足则回退规则（任务书定的上岗线）
MIN_CONFIDENCE = 0.55      # 最大类概率低于此值 → 弃权，回退规则
MIN_OUTCOME_SPEND = 5.0    # 后续窗口花费低于此值 → 不打标签
MIN_BACKTEST_LABELS = 50   # 少于这么多条拒绝判卷（会直接报错）

ACTION_CN = {'pause': '暂停', 'increase_budget': '加预算', 'observe': '观察'}

# ---------- 中文列 → 内部英文键（与 _solvability_v5.py 读同一批列）----------
CSV_FIELDS = {
    'date': '报告开始日期',
    'reach': '覆盖人数',
    'impressions': '展示次数',
    'frequency': '频次',
    'clicks': '链接点击量',
    'cpc': '单次链接点击费用 (USD)',
    'cpm': 'CPM（千次展示费用） (USD)',
    'budget': '广告组预算',
    'btype': '广告组预算类型',
    'spend': '已花费金额 (USD)',
    'add_to_cart': '加入购物车次数',
    'initiate_checkout': '结账发起次数',
    'purchase': '购物次数',
    'add_payment_info': '添加支付信息',
    'purchase_value': '购物转化价值',
}

FEATURE_KEYS = [
    '花费', '收入', '购买', 'ROAS', '花费率',
    'CTR首', 'CTR末', 'CTR斜率%',
    '曝光斜率%', '覆盖首', '覆盖末', '覆盖斜率%',
    'CPM首', 'CPM末', 'CPM斜率%',
    '频次首', '频次末', '频次增幅',
    'CPC', '加购率', '结账率', '支付率', '购买率',
    '日均购买', '单均成本', '客单价',
    '预算', '是系列预算', '前3天购买占比', '首单在第几天',
]

LABELS = ('pause', 'increase_budget', 'observe')


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ==================== 1. 两个适配器 → 内部逐日结构 ====================

def days_from_csv_rows(rows):
    """中文列逐日行 → 内部逐日结构（按日期排序）。

    返回 [{'date','reach','impressions','frequency','clicks','cpc','cpm',
            'budget','btype','spend','add_to_cart','initiate_checkout',
            'purchase','add_payment_info','purchase_value'}, ...]
    """
    out = []
    for r in rows:
        d = {}
        for k, col in CSV_FIELDS.items():
            v = r.get(col)
            if k in ('date', 'btype'):
                d[k] = '' if v is None else str(v)
            else:
                d[k] = fnum(v)
        out.append(d)
    out.sort(key=lambda z: z['date'])
    return out


def days_from_snapshot(snap):
    """make_snapshot 产物 → 内部逐日结构。

    快照的 daily_spend 里缺的字段（如 cpc）按 0 处理；缺整段逐日明细返回 []。
    """
    daily = (snap or {}).get('daily_spend')
    if not daily:
        return []
    btype = str((snap or {}).get('budget_type') or '')
    budget = (snap or {}).get('adset_daily_budget') or (snap or {}).get('campaign_daily_budget') or 0
    out = []
    for d in daily:
        spend = fnum(d.get('spend'))
        imp = fnum(d.get('impressions'))
        clk = fnum(d.get('clicks'))
        out.append({
            'date': str(d.get('date') or ''),
            'reach': fnum(d.get('reach')),
            'impressions': imp,
            'frequency': fnum(d.get('frequency')),
            'clicks': clk,
            'cpc': (spend / clk) if clk else 0.0,
            'cpm': (spend * 1000 / imp) if imp else 0.0,
            'budget': fnum(budget),
            'btype': btype,
            'spend': spend,
            'add_to_cart': fnum(d.get('add_to_cart')),
            'initiate_checkout': fnum(d.get('initiate_checkout')),
            'purchase': fnum(d.get('purchase')),
            'add_payment_info': fnum(d.get('add_payment_info')),
            'purchase_value': fnum(d.get('purchase_value')),
        })
    out.sort(key=lambda z: z['date'])
    return out


# ==================== 2. 唯一的特征实现（口径照抄 _solvability_v5.py）====================

def _features_from_days(rs):
    """30 维可见特征。**这就是 _solvability_v5.py 第 55~91 行的同一口径**，
    改了这里就等于改了考场，历史成绩全部作废 —— 不要顺手「优化」。
    """
    if not rs:
        return None
    d1, d5 = rs[0], rs[-1]
    spend = sum(r['spend'] for r in rs)
    rev = sum(r['purchase_value'] for r in rs)
    pur = sum(r['purchase'] for r in rs)
    clk = sum(r['clicks'] for r in rs)
    atc = sum(r['add_to_cart'] for r in rs)
    ic = sum(r['initiate_checkout'] for r in rs)
    pay = sum(r['add_payment_info'] for r in rs)
    reach1, reach5 = d1['reach'], d5['reach']
    budget = rs[0]['budget']
    nd = len(rs)
    ctr1 = d1['clicks'] / max(1.0, d1['impressions'])
    ctr5 = d5['clicks'] / max(1.0, d5['impressions'])
    imp1, imp5 = d1['impressions'], d5['impressions']
    cpm1, cpm5 = d1['cpm'], d5['cpm']

    return {
        '花费': spend, '收入': rev, '购买': pur, 'ROAS': rev / spend if spend else 0,
        '花费率': spend / (budget * nd) if budget else 0,
        'CTR首': ctr1 * 100, 'CTR末': ctr5 * 100, 'CTR斜率%': (ctr5 - ctr1) / max(1e-9, ctr1) * 100,
        '曝光斜率%': (imp5 - imp1) / max(1.0, imp1) * 100,
        '覆盖首': reach1, '覆盖末': reach5,
        '覆盖斜率%': (reach5 - reach1) / max(1.0, reach1) * 100,
        'CPM首': cpm1, 'CPM末': cpm5, 'CPM斜率%': (cpm5 - cpm1) / max(1e-9, cpm1) * 100,
        '频次首': d1['frequency'], '频次末': d5['frequency'],
        '频次增幅': d5['frequency'] - d1['frequency'],
        'CPC': spend / clk if clk else 0,
        '加购率': atc / clk if clk else 0, '结账率': ic / atc if atc else 0,
        '支付率': pay / ic if ic else 0, '购买率': pur / pay if pay else 0,
        '日均购买': pur / nd, '单均成本': spend / pur if pur else 999,
        '客单价': rev / pur if pur else 0,
        '预算': budget, '是系列预算': 1.0 if '系列' in rs[0]['btype'] else 0.0,
        '前3天购买占比': sum(r['purchase'] for r in rs[:3]) / pur if pur else 0,
        '首单在第几天': next((i + 1 for i, r in enumerate(rs) if r['purchase'] > 0), nd + 1),
    }


def extract_features(rows=None, snapshot=None):
    """取 30 维特征。二选一传参；两个都传时以 rows 为准。

    返回 (feat_dict, feat_vector)。无法取（无逐日明细）返回 (None, None)。
    """
    days = days_from_csv_rows(rows) if rows is not None else days_from_snapshot(snapshot)
    feat = _features_from_days(days)
    if feat is None:
        return None, None
    return feat, [feat[k] for k in FEATURE_KEYS]


def load_csv_grouped(path):
    """把一份 Ads Manager 逐日 CSV 按「广告系列名称」分组。

    返回 Ordered-ish dict: {name: [逐日行, ...]}（行已按日期排序）。
    """
    with io.open(path, encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    g = {}
    for r in rows:
        g.setdefault(r.get('广告系列名称', ''), []).append(r)
    for k in g:
        g[k].sort(key=lambda z: z.get('报告开始日期', ''))
    return g


# ==================== 3. 标签口径 ====================

def derive_label(outcome, min_spend=MIN_OUTCOME_SPEND):
    """按「后续窗口结局」打标签。返回 'pause' / 'increase_budget' / 'observe' / None。

    outcome 需要：spend（后续窗口花费）、net（后续窗口净）、可选 fill_rate（顶格率）。
    """
    if not outcome:
        return None
    spend = outcome.get('spend')
    if spend is None:
        return None
    if float(spend) < float(min_spend):
        return None                      # 后续基本没投，学不到东西
    net = outcome.get('net')
    if net is None:
        return None
    net = float(net)
    if net <= 0:
        return 'pause'
    fr = outcome.get('fill_rate')
    if fr is not None and float(fr) >= FILL_RATE_UP:
        return 'increase_budget'
    return 'observe'


def rule_action(net=None, fill_rate=None, freq_last=None, config=None):
    """冷启动 / 弃权时的回退规则。与 `_guard_cmp.py` 的规则基线、护栏提名线同语义。

    没拿到净额 → observe（不做动作）。这也是**唯一**允许「什么都不做」的地方。
    """
    if net is None:
        return 'observe'
    cfg = config or {}
    if float(net) <= float(cfg.get('kill_stop_net', KILL_STOP_NET)):
        return 'pause'
    if fill_rate is None or freq_last is None:
        return 'observe'
    if (float(fill_rate) >= float(cfg.get('nominate_fill_rate', FILL_RATE_UP))
            and float(freq_last) < float(cfg.get('nominate_freq_max', FREQ_MAX_UP))):
        return 'increase_budget'
    return 'observe'


# ==================== 4. 判卷（与仓库金额口径一致）====================

def score_actions(names, pick, payoff, truth):
    """统一判卷：钱 = Σ 后续 5 天净。

    payoff(label_or_action, truth_row) -> 该动作下的净额。默认：
        pause → net_stop（默认 0，停了就不花不赚）
        increase_budget → net_up
        observe → net_hold
    """
    hit = tp = fp = fn_ = 0
    up_hit = up_tot = 0
    ob_hit = ob_tot = 0
    over = miss = 0.0
    total = 0.0
    n = 0
    for name in names:
        if name not in truth:
            continue
        act = pick(name)
        t = truth[name]
        if act is None:
            continue
        n += 1
        CN = {'pause': '暂停', 'increase_budget': '加预算', 'observe': '观察'}
        if CN[act] == t['正确动作']:
            hit += 1
        if t['正确动作'] == '加预算':
            up_tot += 1
            if act == 'increase_budget':
                up_hit += 1
        if t['正确动作'] == '观察':
            ob_tot += 1
            if act == 'observe':
                ob_hit += 1
        v_act = payoff(act, t)
        total += v_act
        if act == 'pause' and t['正确动作'] == '暂停':
            tp += 1
        elif act == 'pause':
            fp += 1
            over += payoff('observe', t) - payoff('pause', t)
        elif t['正确动作'] == '暂停':
            fn_ += 1
            miss += -payoff('observe', t)
    rec = tp / (tp + fn_) * 100 if tp + fn_ else 0.0
    pre = tp / (tp + fp) * 100 if tp + fp else 0.0
    return {
        'n': n,
        'accuracy': hit / n * 100 if n else 0.0,
        'stop_recall': rec,
        'stop_precision': pre,
        'up_recall': up_hit / up_tot * 100 if up_tot else 0.0,
        'observe_recall': ob_hit / ob_tot * 100 if ob_tot else 0.0,
        'miss_kill_net': -miss,
        'over_kill_net': -over,
        'total_net': total,
    }


def payoff_from_outcome(act, outcome):
    """从 outcome 里的反事实净额取钱（模拟数据有；真实数据缺时退化为 net）。"""
    if act == 'pause':
        v = outcome.get('net_stop')
        return 0.0 if v is None else float(v)
    if act == 'increase_budget':
        v = outcome.get('net_up')
    else:
        v = outcome.get('net_hold')
    if v is None:
        v = outcome.get('net')
    return 0.0 if v is None else float(v)


def truth_payoff(act, t):
    """从真值表行取钱（与 _guard_cmp.py 同一口径）。"""
    if act == 'pause':
        return 0.0
    return fnum(t['加预算_净']) if act == 'increase_budget' else fnum(t['不动_净'])


def load_truth_v5(path=None):
    path = path or os.path.join(D, '真值表-v5.csv')
    with io.open(path, encoding='utf-8-sig') as f:
        return {r['广告']: r for r in csv.DictReader(f)}


# ==================== 5. 表格模型 ====================

class TableJudger:
    """从带标签历史里学「加/停/观察」的判断边界。

    用法：
        tj = TableJudger()
        tj.fit(samples)              # samples: [{'features': {...}, 'label': 'observe'}, ...]
        sugg = tj.judge(snapshots)   # 与 LLM 链路同格式的建议列表

    来不及训练 / 标签不够时，judge() 自动回退规则 `rule_action`，并在 reason 里
    注明「已积累 X/80 条」—— 绝不假装模型已经上岗。
    """

    def __init__(self, min_labels=MIN_LABELS, min_confidence=MIN_CONFIDENCE,
                 n_estimators=300, random_state=1, config=None, store_path=None):
        self.min_labels = int(min_labels)
        self.min_confidence = float(min_confidence)
        self.n_estimators = int(n_estimators)
        self.random_state = int(random_state)
        self.config = dict(config or {})
        self.store_path = store_path or P_STORE
        self.model = None
        self.n_train = 0
        self.label_counts = {}
        self._sk_ok = None

    # ---------- 可用性 ----------
    def _sklearn(self):
        if self._sk_ok is None:
            try:
                from sklearn.ensemble import RandomForestClassifier  # noqa: F401
                self._sk_ok = True
            except ImportError:
                self._sk_ok = False
        return self._sk_ok

    def is_ready(self):
        return self.model is not None and self.n_train >= self.min_labels

    def shortfall(self):
        return max(0, self.min_labels - self.n_train)

    # ---------- 训练 ----------
    def fit(self, samples):
        """samples: [{'features': {...}, 'label': '...'}, ...]。返回 self。

        标签数不足 min_labels 时**不训练**（保留 model=None），调用方走回退。
        """
        usable = [s for s in samples
                  if s.get('label') in LABELS and s.get('features')]
        self.n_train = len(usable)
        counts = {}
        for s in usable:
            counts[s['label']] = counts.get(s['label'], 0) + 1
        self.label_counts = counts
        if self.n_train < self.min_labels:
            self.model = None
            return self
        if not self._sklearn():
            raise RuntimeError(
                '需要 scikit-learn 才能训练表格模型：请先 pip install -r requirements.txt '
                '（当前标签 %d 条已够上岗线，但没有可用的 sklearn）' % self.n_train)
        from sklearn.ensemble import RandomForestClassifier
        X = [[s['features'].get(k, 0.0) for k in FEATURE_KEYS] for s in usable]
        y = [s['label'] for s in usable]
        self.model = RandomForestClassifier(
            n_estimators=self.n_estimators,
            class_weight='balanced_subsample',
            random_state=self.random_state,
        )
        self.model.fit(X, y)
        return self

    def load_from_store(self, path=None):
        """从 learning_store.jsonl 读已标注样本并训练。"""
        path = path or self.store_path
        samples = []
        if not os.path.exists(path):
            self.n_train = 0
            return self
        with io.open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                outcome = rec.get('outcome')
                label = (outcome or {}).get('label') if outcome else None
                if label is None:
                    label = derive_label(outcome)
                if label and rec.get('features'):
                    samples.append({'features': rec['features'], 'label': label})
        return self.fit(samples)

    # ---------- 判断 ----------
    def _rule_suggestion(self, snap, note=''):
        nv = (snap or {}).get('net_view') or {}
        act = rule_action(nv.get('net'), nv.get('fill_rate'), nv.get('freq_last'), self.config)
        tail = ('【规则回退】标签 %d/%d 条，未达上岗线；本条按规则给「%s」%s'
                % (self.n_train, self.min_labels, ACTION_CN.get(act, act),
                   ('（' + note + '）') if note else ''))
        s = {
            'campaign_id': str(snap.get('id')),
            'action': act,
            'budget_change_pct': 0,
            'reason': tail,
            'source': 'rule_fallback',
            'model_confidence': None,
        }
        if act == 'increase_budget':
            s = _as_nomination(s, '规则回退提名')
        return s

    def judge(self, snapshots, config=None, apply_guardrail=True):
        """对一批快照出建议。返回与 LLM 链路同格式的 suggestions 列表。"""
        cfg = dict(self.config)
        cfg.update(config or {})
        out = []
        for snap in (snapshots or []):
            if not snap or snap.get('id') is None:
                continue
            if not self.is_ready():
                out.append(self._rule_suggestion(snap))
                continue
            feat, vec = extract_features(snapshot=snap)
            if feat is None:
                out.append(self._rule_suggestion(snap, '无逐日明细'))
                continue
            proba = self.model.predict_proba([vec])[0]
            # 手写 argmax，不调 numpy 的 .argmax() —— 这样任何「像概率数组」的对象
            # （list / 自定义类）都能用，测试可以精确制造低置信度情形。
            i = max(range(len(proba)), key=lambda j: float(proba[j]))
            conf = float(proba[i])
            act = str(self.model.classes_[i])
            if conf < self.min_confidence:
                out.append(self._rule_suggestion(
                    snap, '模型置信度 %.2f < %.2f 弃权' % (conf, self.min_confidence)))
                continue
            s = {
                'campaign_id': str(snap.get('id')),
                'action': act,
                'budget_change_pct': 0,
                'reason': ('【表格模型】置信度 %.2f（训练样本 %d 条）'
                           % (conf, self.n_train)),
                'source': 'table_model',
                'model_confidence': round(conf, 3),
            }
            if act == 'increase_budget':
                s = _as_nomination(s, '模型建议加预算')
            out.append(s)
        if apply_guardrail:
            try:
                from fb_ai_engine import enforce_risk_guardrails
                out, _ = enforce_risk_guardrails(out, snapshots, cfg)
            except ImportError:
                pass     # 单独跑本模块（无 fb_ai_engine）时跳过；生产链路里一定会跑
        return out

    # ---------- 判卷 ----------
    def backtest(self, samples, truth=None, folds=5, random_state=7, payoff=None):
        """5 折**折外**判卷（StratifiedKFold, shuffle）。标签 <50 条直接报错。

        samples: [{'features': {...}, 'label': '...', 'outcome': {...}, 'ad_name': '...'}]
        truth:   可选，{广告名: 真值表行}；给了就用真值表的钱口径判卷，否则用 outcome 的反事实净。
        返回成绩单 dict。
        """
        usable = [s for s in samples if s.get('label') in LABELS and s.get('features')]
        if len(usable) < MIN_BACKTEST_LABELS:
            raise ValueError(
                '标签只有 %d 条，少于判卷下限 %d 条，拒绝判卷（样本太少，成绩无意义）。'
                % (len(usable), MIN_BACKTEST_LABELS))
        if not self._sklearn():
            raise RuntimeError('需要 scikit-learn 才能判卷，请先 pip install -r requirements.txt')
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import StratifiedKFold

        X = [[s['features'].get(k, 0.0) for k in FEATURE_KEYS] for s in usable]
        y = [s['label'] for s in usable]
        skf = StratifiedKFold(n_splits=folds, shuffle=True, random_state=random_state)

        oof = [None] * len(usable)
        confs = [None] * len(usable)
        for tr, te in skf.split(X, y):
            m = RandomForestClassifier(
                n_estimators=self.n_estimators,
                class_weight='balanced_subsample',
                random_state=self.random_state)
            m.fit([X[i] for i in tr], [y[i] for i in tr])
            pr = m.predict_proba([X[i] for i in te])
            for j, i in enumerate(te):
                k = max(range(len(pr[j])), key=lambda q: float(pr[j][q]))
                oof[i] = str(m.classes_[k])
                confs[i] = float(pr[j][k])

        # 弃权回退（置信度低 → 用规则），保持与 judge() 同一行为
        use_rule = [False] * len(usable)
        for i in range(len(usable)):
            if confs[i] is not None and confs[i] < self.min_confidence:
                use_rule[i] = True

        idx = {s.get('ad_name'): i for i, s in enumerate(usable) if s.get('ad_name')}

        def pick_by_name(name):
            i = idx.get(name)
            if i is None:
                return None
            if use_rule[i]:
                nv = (usable[i].get('features') or {})
                return rule_action(
                    net=_visible_net(usable[i]), fill_rate=_visible_fill(usable[i]),
                    freq_last=_visible_freq(usable[i]), config=self.config)
            return oof[i]

        if truth:
            names = [n for n in truth if n in idx]
            res = score_actions(names, pick_by_name, truth_payoff, truth)
        else:
            by_name = {s['ad_name']: s for s in usable if s.get('ad_name')}
            names = [n for n in by_name if n in idx]

            def pick_no_truth(name):
                return pick_by_name(name)

            res = _score_by_outcome(names, pick_no_truth, by_name)
        res['folds'] = folds
        res['abstain'] = sum(1 for f in use_rule if f)
        counts = {}
        for lab in y:
            counts[lab] = counts.get(lab, 0) + 1
        res['label_counts'] = counts
        return res


def _visible_net(sample):
    """从样本里取「可见净」用于弃权回退（优先 outcome 里的可见窗口，其次特征）。"""
    o = sample.get('outcome') or {}
    for k in ('visible_net', 'net_visible'):
        if o.get(k) is not None:
            return float(o[k])
    f = sample.get('features') or {}
    if '花费' in f and '收入' in f:
        return float(f['收入']) - float(f['花费'])
    return None


def _visible_fill(sample):
    o = sample.get('outcome') or {}
    if o.get('visible_fill_rate') is not None:
        return float(o['visible_fill_rate'])
    f = sample.get('features') or {}
    return float(f['花费率']) if f.get('花费率') is not None else None


def _visible_freq(sample):
    o = sample.get('outcome') or {}
    if o.get('visible_freq_last') is not None:
        return float(o['visible_freq_last'])
    f = sample.get('features') or {}
    return float(f['频次末']) if f.get('频次末') is not None else None


def _score_by_outcome(names, pick, by_name):
    """没有真值表时，用 outcome 里的反事实净判卷。"""
    hit = tp = fp = fn_ = up_hit = up_tot = 0
    ob_hit = ob_tot = 0
    over = miss = 0.0
    total = 0.0
    for name in names:
        s = by_name[name]
        act = pick(name)
        if act is None:
            continue
        o = s.get('outcome') or {}
        lab = s['label']
        if act == lab:
            hit += 1
        if lab == 'increase_budget':
            up_tot += 1
            if act == 'increase_budget':
                up_hit += 1
        if lab == 'observe':
            ob_tot += 1
            if act == 'observe':
                ob_hit += 1
        total += payoff_from_outcome(act, o)
        if act == 'pause' and lab == 'pause':
            tp += 1
        elif act == 'pause':
            fp += 1
            over += payoff_from_outcome('observe', o) - payoff_from_outcome('pause', o)
        elif lab == 'pause':
            fn_ += 1
            miss += -payoff_from_outcome('observe', o)
    n = len(names)
    return {
        'n': n,
        'accuracy': hit / n * 100 if n else 0.0,
        'stop_recall': tp / (tp + fn_) * 100 if tp + fn_ else 0.0,
        'stop_precision': tp / (tp + fp) * 100 if tp + fp else 0.0,
        'up_recall': up_hit / up_tot * 100 if up_tot else 0.0,
        'observe_recall': ob_hit / ob_tot * 100 if ob_tot else 0.0,
        'miss_kill_net': -miss,
        'over_kill_net': -over,
        'total_net': total,
    }


def _as_nomination(s, why=''):
    """把一条「加预算」建议改写成提名形态：动作仍是加预算，但**不直接执行**。

    仓库的提名制语义：带 nomination 字段 + budget_change_pct=0 = 只交人工复核。
    """
    s['budget_change_pct'] = 0
    s['nomination'] = {
        'suggested_action': 'increase_budget',
        'rule': why,
        'applied': False,
    }
    s['reason'] = s.get('reason', '') + '｜【提名】建议加预算，按提名制交人工批准，未直接执行'
    return s
