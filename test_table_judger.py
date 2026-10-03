# -*- coding: utf-8 -*-
"""test_table_judger.py —— 「会学习的判断层」的测试。

跑法：
    python test_table_judger.py            # 全部用例
    python test_table_judger.py -v         # 带用例名

设计原则（与仓库其它脚本一致）：
  · 断言**可观察副作用**（存盘条数、最终 action、理由文本），不断言内部返回值的形状；
  · 特征口径用**独立实现**逐格比对，而不是自己跟自己比；
  · 需要 sklearn 的用例在没有 sklearn 时 skip，而不是假装通过。
"""
import csv
import io
import json
import os
import sys
import tempfile
import time
import unittest

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)

import flywheel                                     # noqa: E402
import metric_views as mv                           # noqa: E402
import table_judger as tj                           # noqa: E402
import fb_ai_engine as eng                          # noqa: E402
import fb_test_loader as lo                         # noqa: E402

try:
    import sklearn                                    # noqa: F401
    HAS_SK = True
except ImportError:
    HAS_SK = False

P_DATA = os.path.join(D, '模拟广告数据-100广告10天-v5.csv')
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_HID = os.path.join(D, '真值表-逐日-v5.csv')


# ==================== 独立实现（用于特征口径比对）====================

def _independent_features(rows):
    """**故意重写一遍** _solvability_v5.py 第 55~91 行的算法。

    如果 table_judger 的适配器把字段名映射错了（比如「覆盖人数」当成「展示次数」），
    这里就会对不上。自己跟自己比是查不出映射错误的。
    """
    import csv
    rs = sorted(rows, key=lambda z: z['报告开始日期'])

    def f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    d1, d5 = rs[0], rs[-1]
    spend = sum(f(r['已花费金额 (USD)']) for r in rs)
    rev = sum(f(r['购物转化价值']) for r in rs)
    pur = sum(f(r['购物次数']) for r in rs)
    clk = sum(f(r['链接点击量']) for r in rs)
    atc = sum(f(r['加入购物车次数']) for r in rs)
    ic = sum(f(r['结账发起次数']) for r in rs)
    pay = sum(f(r['添加支付信息']) for r in rs)
    reach1, reach5 = f(d1['覆盖人数']), f(d5['覆盖人数'])
    budget = f(rs[0]['广告组预算'])
    nd = len(rs)
    ctr1 = f(d1['链接点击量']) / max(1, f(d1['展示次数']))
    ctr5 = f(d5['链接点击量']) / max(1, f(d5['展示次数']))
    imp1, imp5 = f(d1['展示次数']), f(d5['展示次数'])
    cpm1 = f(d1['CPM（千次展示费用） (USD)'])
    cpm5 = f(d5['CPM（千次展示费用） (USD)'])
    return {
        '花费': spend, '收入': rev, '购买': pur, 'ROAS': rev / spend if spend else 0,
        '花费率': spend / (budget * nd) if budget else 0,
        'CTR首': ctr1 * 100, 'CTR末': ctr5 * 100, 'CTR斜率%': (ctr5 - ctr1) / max(1e-9, ctr1) * 100,
        '曝光斜率%': (imp5 - imp1) / max(1.0, imp1) * 100,
        '覆盖首': reach1, '覆盖末': reach5,
        '覆盖斜率%': (reach5 - reach1) / max(1.0, reach1) * 100,
        'CPM首': cpm1, 'CPM末': cpm5, 'CPM斜率%': (cpm5 - cpm1) / max(1e-9, cpm1) * 100,
        '频次首': f(d1['频次']), '频次末': f(d5['频次']),
        '频次增幅': f(d5['频次']) - f(d1['频次']),
        'CPC': spend / clk if clk else 0,
        '加购率': atc / clk if clk else 0, '结账率': ic / atc if atc else 0,
        '支付率': pay / ic if ic else 0, '购买率': pur / pay if pay else 0,
        '日均购买': pur / nd, '单均成本': spend / pur if pur else 999,
        '客单价': rev / pur if pur else 0,
        '预算': budget, '是系列预算': 1.0 if '系列' in rs[0]['广告组预算类型'] else 0.0,
        '前3天购买占比': sum(f(r['购物次数']) for r in rs[:3]) / pur if pur else 0,
        '首单在第几天': next((i + 1 for i, r in enumerate(rs) if f(r['购物次数']) > 0), nd + 1),
    }


def _read_csv(path):
    import csv
    with io.open(path, encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


class FakeModel:
    """伪模型：用来精确制造「低置信度」这一情形。"""

    def __init__(self, classes, proba):
        self.classes_ = classes
        self._p = proba

    def predict_proba(self, X):
        return [self._p for _ in X]


def mk_snapshot(sid, net=None, fill=0.5, freq=1.2, days=5, spend=10.0, rev=5.0):
    """造一个最小可用快照（带 net_view）。"""
    daily = []
    for i in range(days):
        daily.append({'date': '2026-09-2%d' % (i + 3), 'spend': spend / days,
                      'purchase': 1, 'purchase_value': rev / days,
                      'impressions': 100, 'clicks': 5, 'reach': 90,
                      'frequency': freq, 'add_to_cart': 1,
                      'initiate_checkout': 1, 'add_payment_info': 1})
    snap = {'id': sid, 'name': 'ad-%s' % sid, 'budget_type': 'ABO',
            'adset_daily_budget': 10, 'daily_spend': daily}
    if net is not None:
        snap['net_view'] = {'days': days, 'spend': spend, 'revenue': rev,
                            'net': net, 'roas': (rev / spend) if spend else 0,
                            'active_days': days, 'fill_rate': fill, 'freq_last': freq}
    return snap


class TestFeatures(unittest.TestCase):
    """特征口径：与独立实现逐格比对（能抓住字段映射错误）。"""

    def test_feature_parity_all_ads(self):
        rows = _read_csv(P_DATA)
        g = {}
        for r in rows:
            g.setdefault(r['广告系列名称'], []).append(r)
        self.assertEqual(len(g), 100, '模拟数据应有 100 条广告')

        bad = []
        for name, rs in g.items():
            want = _independent_features(rs)
            got, _ = tj.extract_features(rows=rs)
            self.assertIsNotNone(got, name)
            self.assertEqual(set(got.keys()), set(want.keys()), name)
            for k in want:
                if abs(float(got[k]) - float(want[k])) > 1e-6:
                    bad.append((name, k, got[k], want[k]))
        self.assertEqual(bad, [], '特征与 _solvability_v5 口径不一致：%s' % bad[:5])

    def test_feature_vector_order_matches_keys(self):
        rows = _read_csv(P_DATA)
        rs = [r for r in rows if r['广告系列名称'] == rows[0]['广告系列名称']]
        feat, vec = tj.extract_features(rows=rs)
        self.assertEqual(len(vec), len(tj.FEATURE_KEYS))
        for k, v in zip(tj.FEATURE_KEYS, vec):
            self.assertAlmostEqual(float(v), float(feat[k]), places=9)

    def test_snapshot_adapter_matches_csv_adapter(self):
        """快照适配器（生产路径）与 CSV 适配器（离线路径）必须给出同一把尺子。"""
        rows = _read_csv(P_DATA)
        name = rows[0]['广告系列名称']
        rs = [r for r in rows if r['广告系列名称'] == name]
        feat_csv, _ = tj.extract_features(rows=rs)
        sn = eng.make_snapshot([{
            'id': 'x', 'name': name, 'budget_type': 'ABO',
            'adset_daily_budget': float(rs[0]['广告组预算']),
            'daily_spend': [{
                'date': r['报告开始日期'], 'spend': float(r['已花费金额 (USD)']),
                'purchase': int(float(r['购物次数'])),
                'purchase_value': float(r['购物转化价值']),
                'impressions': int(float(r['展示次数'])), 'clicks': int(float(r['链接点击量'])),
                'reach': int(float(r['覆盖人数'])), 'frequency': float(r['频次']),
                'add_to_cart': int(float(r['加入购物车次数'])),
                'initiate_checkout': int(float(r['结账发起次数'])),
                'add_payment_info': int(float(r['添加支付信息'])),
            } for r in rs],
        }])[0]
        feat_snap, _ = tj.extract_features(snapshot=sn)
        # 容差说明：CSV 路径读的是「导出的 CPM/CTR 列」，快照路径只能用
        # 花费/展示**重算** —— 生成器写列时与重算存在 1e-4 量级差异。
        # 这是数据源差异，不是适配器写错；映射错的话差值会是数量级而不是 1e-4。
        # 「斜率%」「xx率」是把小差异做比值后放大的派生量（除以 ~12 放大 8 倍），
        # 所以对这一类给相对容差，其余给绝对容差。
        for k in tj.FEATURE_KEYS:
            a, b = float(feat_csv[k]), float(feat_snap[k])
            if '斜率' in k or k.endswith('率'):
                tol = max(0.02, abs(a) * 5e-3)
            else:
                tol = max(0.01, abs(a) * 1e-4)
            self.assertLessEqual(abs(a - b), tol,
                                 msg='快照/CSV 两条路对不上：%s (%r vs %r)' % (k, a, b))


class TestDeriveLabel(unittest.TestCase):
    def test_four_cases(self):
        # ① 后续没花钱 → 不打标签
        self.assertIsNone(tj.derive_label({'spend': 1.0, 'net': -3.0, 'fill_rate': 0.2}))
        self.assertIsNone(tj.derive_label(None))
        self.assertIsNone(tj.derive_label({}))
        # ② 净 ≤ 0 → 停
        self.assertEqual(tj.derive_label({'spend': 30, 'net': -5.0, 'fill_rate': 0.9}), 'pause')
        self.assertEqual(tj.derive_label({'spend': 30, 'net': 0.0, 'fill_rate': 0.9}), 'pause')
        # ③ 净 > 0 且顶格率 ≥ 0.90 → 加预算
        self.assertEqual(tj.derive_label({'spend': 30, 'net': 12.0, 'fill_rate': 0.90}),
                         'increase_budget')
        # ④ 其余 → 观察
        self.assertEqual(tj.derive_label({'spend': 30, 'net': 12.0, 'fill_rate': 0.89}), 'observe')
        self.assertEqual(tj.derive_label({'spend': 30, 'net': 12.0}), 'observe')


class TestRuleFallback(unittest.TestCase):
    def test_rule_action_semantics(self):
        self.assertEqual(tj.rule_action(None), 'observe')
        self.assertEqual(tj.rule_action(-1.0, 0.5, 1.2), 'pause')
        self.assertEqual(tj.rule_action(5.0, 0.95, 1.2), 'increase_budget')
        self.assertEqual(tj.rule_action(5.0, 0.95, 1.5), 'observe')   # 频次高，受众看腻了
        self.assertEqual(tj.rule_action(5.0, 0.5, 1.2), 'observe')    # 没顶格，预算不是瓶颈

    def test_fallback_when_labels_short(self):
        judge = tj.TableJudger(min_labels=80)
        judge.fit([{'features': {k: 0.0 for k in tj.FEATURE_KEYS}, 'label': 'observe'}
                   for _ in range(10)])
        self.assertFalse(judge.is_ready())
        self.assertEqual(judge.shortfall(), 70)
        sugg = judge.judge([mk_snapshot('1', net=5.0)])
        self.assertEqual(len(sugg), 1)
        self.assertEqual(sugg[0]['source'], 'rule_fallback')
        self.assertIn('10/80', sugg[0]['reason'])


class TestGuardrailOverride(unittest.TestCase):
    """护栏必须能覆盖模型输出（这是整个分工的底线）。"""

    def test_net_le_zero_forced_pause(self):
        # ⚠️ 夹具必须给 fill=1.0 / freq=3.2：默认的 fill=0.5、freq=1.2 是**探索期形态**
        #    （预算没花出去、受众还没看腻），新规则会正当性地不拦 —— 那时测的就不是
        #    「护栏能否覆盖模型」而是「探索期是否被识别」了，两件事要分开测。
        snap = mk_snapshot('1', net=-7.5, spend=20.0, rev=12.5, fill=1.0, freq=3.2)
        sugg = [{'campaign_id': '1', 'action': 'increase_budget',
                 'budget_change_pct': 20, 'reason': '模型想加预算'}]
        out, stats = eng.enforce_risk_guardrails(sugg, [snap], {})
        self.assertEqual(out[0]['action'], 'pause')
        self.assertEqual(out[0]['guardrail'], 'forced_pause')
        self.assertEqual(out[0]['ai_action'], 'increase_budget')   # 原判留痕
        self.assertEqual(out[0]['budget_change_pct'], 0)
        self.assertEqual(stats['forced_pause'], 1)

    def test_exploration_phase_ad_is_not_force_paused(self):
        """探索期（顶格率低 + 频次低）：护栏不越权，落到 observe 而不是 forced_pause。"""
        snap = mk_snapshot('9', net=-7.5, spend=20.0, rev=12.5, fill=0.5, freq=1.2)
        sugg = [{'campaign_id': '9', 'action': 'increase_budget',
                 'budget_change_pct': 20, 'reason': '模型想加预算'}]
        out, stats = eng.enforce_risk_guardrails(sugg, [snap], {})
        self.assertEqual(out[0]['action'], 'observe')
        self.assertEqual(out[0]['guardrail'], 'blocked_by_precondition')
        self.assertEqual(stats['forced_pause'], 0)
        self.assertEqual(stats['blocked_by_precondition'], 1)
        self.assertIn('探索期', out[0]['reason'])

    def test_net_positive_blocks_kill(self):
        snap = mk_snapshot('2', net=9.9, spend=10.0, rev=19.9)
        sugg = [{'campaign_id': '2', 'action': 'pause', 'budget_change_pct': 0,
                 'reason': '模型想停'}]
        out, stats = eng.enforce_risk_guardrails(sugg, [snap], {})
        self.assertEqual(out[0]['action'], 'observe')
        self.assertEqual(out[0]['guardrail'], 'blocked_kill')
        self.assertEqual(out[0]['ai_action'], 'pause')
        self.assertEqual(stats['blocked_kill'], 1)

    def test_judge_output_always_passes_guardrail(self):
        """端到端：judge() 产出的建议，亏钱的必被停、赚钱的不许被停。"""
        judge = tj.TableJudger(min_labels=80)      # 未上岗 → 走规则回退
        snaps = [mk_snapshot('a', net=-4.0, spend=10, rev=6),
                 mk_snapshot('b', net=8.0, fill=0.4, freq=1.2, spend=10, rev=18)]
        out = judge.judge(snaps)
        by = {s['campaign_id']: s for s in out}
        self.assertEqual(by['a']['action'], 'pause')
        self.assertEqual(by['b']['action'], 'observe')

    def test_nomination_never_executes(self):
        """加预算只能是提名：带 nomination 且 budget_change_pct 必须为 0。"""
        judge = tj.TableJudger(min_labels=80)
        out = judge.judge([mk_snapshot('c', net=12.0, fill=0.95, freq=1.1)])
        s = out[0]
        self.assertEqual(s['action'], 'increase_budget')
        self.assertEqual(s['budget_change_pct'], 0)
        self.assertTrue(s.get('nomination'))
        self.assertFalse(s['nomination']['applied'])
        self.assertIn('提名', s['reason'])

    # ---- 中文状态词归一（2026-10-04 12条14天 GUI 实测抓到的生产 bug）----
    # 导出列是「投放中/未投放」，护栏只认 'ACTIVE' ⇒ 「投放中」被当成「不在投」：
    # 该强停的不敢停（理由还自相矛盾），提名被整体跳过（nominated=0）。

    def test_chinese_delivering_status_counts_as_active(self):
        snap = mk_snapshot('11', net=-7.5, spend=20.0, rev=12.5, fill=1.0, freq=3.2)
        snap['status'] = '投放中'
        sugg = [{'campaign_id': '11', 'action': 'increase_budget',
                 'budget_change_pct': 20, 'reason': '模型想加预算'}]
        out, stats = eng.enforce_risk_guardrails(sugg, [snap], {})
        self.assertEqual(out[0]['action'], 'pause')
        self.assertEqual(out[0]['guardrail'], 'forced_pause')
        self.assertNotIn('不在投', out[0]['reason'])

    def test_chinese_delivering_ad_is_nomination_eligible(self):
        """同一根因的另一面：状态词不认 ⇒ 提名检查被整体跳过（nominated=0）。

        夹具 = 教科书「该加钱」形态（净赚 + 顶格 1.0 + 频次 2.2 + 5 天）。"""
        snap = mk_snapshot('1', net=50.0, spend=20.0, rev=70.0, fill=1.0, freq=2.2)
        snap['status'] = '投放中'
        sugg = [{'campaign_id': '1', 'action': 'observe', 'budget_change_pct': 0,
                 'reason': '模型观望'}]
        out, stats = eng.enforce_risk_guardrails(sugg, [snap], {})
        self.assertEqual(stats['nominated'], 1)
        self.assertIn('提名·加预算', out[0]['reason'])

    def test_chinese_not_delivering_still_blocks(self):
        """「未投放」必须仍然算不在投（归一不能把没在花的钱放进来）。"""
        snap = mk_snapshot('12', net=-7.5, spend=20.0, rev=12.5, fill=1.0, freq=3.2)
        snap['status'] = '未投放'
        sugg = [{'campaign_id': '12', 'action': 'increase_budget',
                 'budget_change_pct': 20, 'reason': '模型想加预算'}]
        out, stats = eng.enforce_risk_guardrails(sugg, [snap], {})
        self.assertEqual(out[0]['action'], 'observe')
        self.assertEqual(out[0]['guardrail'], 'blocked_by_precondition')
        self.assertIn('不在投', out[0]['reason'])

    def test_learning_phase_status_counts_as_active(self):
        """「学习期」是在真花钱的（Meta 学习期 ≠ 没在投），必须能被强停。"""
        snap = mk_snapshot('13', net=-7.5, spend=20.0, rev=12.5, fill=1.0, freq=3.2)
        snap['status'] = '学习期'
        sugg = [{'campaign_id': '13', 'action': 'increase_budget',
                 'budget_change_pct': 20, 'reason': '模型想加预算'}]
        out, stats = eng.enforce_risk_guardrails(sugg, [snap], {})
        self.assertEqual(out[0]['guardrail'], 'forced_pause')

    def test_net_positive_decrease_budget_passes_through(self):
        """减预算不是关停（2026-10-04 验收）：净额为正时禁杀线只禁 pause。

        AD010 形态：净 +48 在赚，但近 3 天 0 单、ROAS 1.06 —— AI 判减预算应原样通过，
        且减着的钱不再叠加加预算提名（减着钱还提名加钱是自相矛盾）。"""
        snap = mk_snapshot('10', net=48.0, spend=20.0, rev=68.0, fill=1.0, freq=3.2)
        sugg = [{'campaign_id': '10', 'action': 'decrease_budget', 'budget_change_pct': 30,
                 'reason': '近三天零单，先减 30%'}]
        out, stats = eng.enforce_risk_guardrails(sugg, [snap], {})
        self.assertEqual(out[0]['action'], 'decrease_budget')
        self.assertNotIn('guardrail', out[0])
        self.assertNotIn('提名', out[0]['reason'])


class TestAbstain(unittest.TestCase):
    def test_low_confidence_abstains_to_rule(self):
        judge = tj.TableJudger(min_labels=80, min_confidence=0.55)
        judge.model = FakeModel(['observe', 'pause', 'increase_budget'], [0.40, 0.35, 0.25])
        judge.n_train = 100
        self.assertTrue(judge.is_ready())
        out = judge.judge([mk_snapshot('z', net=-3.0)])
        self.assertEqual(out[0]['source'], 'rule_fallback')
        self.assertIn('弃权', out[0]['reason'])
        self.assertEqual(out[0]['action'], 'pause')      # 回退规则：净≤0 该停

    def test_high_confidence_uses_model(self):
        judge = tj.TableJudger(min_labels=80, min_confidence=0.55)
        judge.model = FakeModel(['observe', 'pause', 'increase_budget'], [0.05, 0.05, 0.90])
        judge.n_train = 100
        out = judge.judge([mk_snapshot('y', net=3.0, fill=0.95, freq=1.1)])
        self.assertEqual(out[0]['source'], 'table_model')
        self.assertEqual(out[0]['action'], 'increase_budget')


class TestStore(unittest.TestCase):
    def test_same_ad_same_day_written_once(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'store.jsonl')
            snap = mk_snapshot('77', net=1.0)
            self.assertTrue(flywheel.record_snapshot(snap, 'observe', path=p))
            self.assertFalse(flywheel.record_snapshot(snap, 'observe', path=p))
            recs = flywheel.load_store(p)
            self.assertEqual(len(recs), 1, '同广告同天只能有一条')
            self.assertEqual(recs[0]['ad_id'], '77')
            self.assertEqual(recs[0]['tool_action'], 'observe')
            self.assertIsNone(recs[0]['outcome'])
            self.assertEqual(len(recs[0]['features']), len(tj.FEATURE_KEYS))

    def test_backfill_writes_labels_and_stats(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'store.jsonl')
            added, updated = flywheel.backfill_from_truth(P_DATA, P_TRUTH, p, P_HID)
            self.assertEqual(added, 100)
            self.assertEqual(updated, 0)
            st = flywheel.stats(p)
            self.assertEqual(st['snapshots'], 100)
            self.assertGreaterEqual(st['labeled'], 50)
            self.assertTrue(st['ready'])
            # 幂等：再回填一次不该新增
            added2, updated2 = flywheel.backfill_from_truth(P_DATA, P_TRUTH, p, P_HID)
            self.assertEqual(added2, 0)
            self.assertEqual(updated2, 100)

    def test_real_mode_has_no_counterfactual(self):
        """真实模式必须诚实标记：没有反事实。"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'store.jsonl')
            recs = flywheel.build_records_from_window(P_DATA, head_days=3)
            self.assertTrue(recs)
            o = recs[0]['outcome']
            self.assertFalse(o['counterfactual'])
            self.assertEqual(o['source'], 'real')
            self.assertEqual(o['net_hold'], o['net_up'])
            flywheel.backfill_from_window(P_DATA, p, head_days=3)
            self.assertEqual(len(flywheel.load_store(p)), len(recs))


class TestDataReconciliation(unittest.TestCase):
    """数据的对账关系：证明我们对三份文件的理解是对的。"""

    def test_visible_spend_equals_truth_total(self):
        truth = {r['广告']: r for r in _read_csv(P_TRUTH)}
        g = {}
        for r in _read_csv(P_DATA):
            g.setdefault(r['广告系列名称'], []).append(r)
        for name, rs in g.items():
            if name not in truth:
                continue
            sp = sum(float(r['已花费金额 (USD)']) for r in rs)
            # 容差 0.03：逐日列是四舍五入到 2 位后写盘的，5 天相加最多差 0.025
            self.assertAlmostEqual(sp, float(truth[name]['总花费']), delta=0.03,
                                   msg='可见窗口花费应等于真值表「总花费」：%s' % name)

    def test_tail_hold_net_equals_truth(self):
        truth = {r['广告']: r for r in _read_csv(P_TRUTH)}
        g = {}
        for r in _read_csv(P_HID):
            if r['方案'] == '不动':
                g.setdefault(r['广告'], []).append(r)
        for name, rs in g.items():
            if name not in truth:
                continue
            net = sum(float(r['期望净']) for r in rs)
            # 同样容差 0.03（逐日期望净 round 到 2 位后相加）
            self.assertAlmostEqual(net, float(truth[name]['不动_净']), delta=0.03,
                                   msg='后续窗口「不动」净应等于真值表 不动_净：%s' % name)


@unittest.skipUnless(HAS_SK, '需要 scikit-learn（pip install -r requirements.txt）')
class TestBacktest(unittest.TestCase):
    def _synthetic(self, n=150):
        kinds = ['observe', 'increase_budget', 'pause']
        prof = {
            'observe':         dict(roas=1.0, buy=5, per_day=1.0, fill=0.70, net=5.0),
            'increase_budget': dict(roas=5.0, buy=12, per_day=2.4, fill=0.95, net=30.0),
            'pause':           dict(roas=0.1, buy=0, per_day=0.0, fill=0.50, net=-10.0),
        }
        out = []
        for i in range(n):
            k = kinds[i % 3]
            p = prof[k]
            f = {key: 0.0 for key in tj.FEATURE_KEYS}
            f['ROAS'] = p['roas'] + (i % 5) * 0.01
            f['购买'] = float(p['buy'])
            f['日均购买'] = p['per_day']
            f['花费率'] = p['fill']
            f['频次末'] = 1.2
            out.append({
                'features': f, 'label': k, 'ad_name': 'ad%03d' % i,
                'outcome': {'net': p['net'], 'net_hold': p['net'], 'net_up': p['net'],
                            'net_stop': 0.0, 'spend': 20.0,
                            'visible_net': p['net'], 'visible_fill_rate': p['fill'],
                            'visible_freq_last': 1.2},
            })
        return out

    def test_backtest_on_separable_data(self):
        judge = tj.TableJudger(min_labels=80)
        res = judge.backtest(self._synthetic(150), truth=None, folds=5)
        self.assertGreater(res['accuracy'], 90.0,
                           '合成可分数据上准确率应 >90%%，实际 %.1f%%' % res['accuracy'])
        self.assertEqual(res['n'], 150)
        self.assertIn('observe_recall', res)
        self.assertIn('up_recall', res)

    def test_backtest_rejects_tiny_sample(self):
        judge = tj.TableJudger(min_labels=10)
        with self.assertRaises(ValueError) as cm:
            judge.backtest(self._synthetic(30)[:30], truth=None)
        self.assertIn('拒绝判卷', str(cm.exception))

    def test_backtest_matches_rule_on_real_data(self):
        """真数据上跑一次判卷：必须能跑完、给出四项召回与会合计净。"""
        samples = flywheel.records_to_samples(
            flywheel.build_records_from_truth(P_DATA, P_TRUTH, P_HID))
        self.assertEqual(len(samples), 100)
        truth = tj.load_truth_v5(P_TRUTH)
        judge = tj.TableJudger(min_labels=80)
        res = judge.backtest(samples, truth=truth, folds=5)
        self.assertEqual(res['n'], 100)
        self.assertGreater(res['accuracy'], 45.0)       # 高于「什么都不做」下限
        self.assertGreater(res['total_net'], 8000.0)    # 后段总净为正
        self.assertGreaterEqual(res['stop_recall'], 0.0)


P_SIM = os.path.join(D, '模拟广告数据-100广告10天-v5.csv')

# 用户 Ads Manager「导出」的原始表头（2026-10-02 实测，24 列）。
REAL_EXPORT_HEADER = [
    '报告开始日期', '报告结束日期', '广告系列名称', '广告系列投放', '归因设置',
    '成效', '成效指标', '覆盖人数', '展示次数', '频次', '链接点击量',
    '单次链接点击费用 (USD)', 'CPM（千次展示费用） (USD)', '广告组预算', '广告组预算类型',
    '已花费金额 (USD)', '加入购物车次数', '结账发起次数', '购物次数', '添加支付信息',
    '购物转化价值', '广告花费回报 (ROAS) - 购物', '结果（初始）', '成效（初始）指标',
]


def _write_csv(header, rows=None):
    """写一个只有表头（和一行 0）的临时 CSV，返回路径。"""
    fd, p = tempfile.mkstemp(suffix='.csv')
    os.close(fd)
    with io.open(p, 'w', encoding='utf-8-sig', newline='') as f:
        f.write(','.join('"%s"' % h for h in header) + '\n')
        if rows:
            for r in rows:
                f.write(','.join(str(x) for x in r) + '\n')
        else:
            f.write(','.join('0' for _ in header) + '\n')
    return p


class TestColumns(unittest.TestCase):
    """列名映射与缺列行为 —— 保护「数据能不能喂进来」这条命脉。

    背景（2026-10-02）：原来只认 Ads Manager 的一种长列名，且缺列会静默变成 0，
    于是「这项没读到」被当成「这项是 0」，判断结果悄悄失真而没有任何提示。
    """

    def test_real_export_header_resolves_all(self):
        """真实 Ads Manager 导出的 24 列表头，15 个必需列必须全部认出来。"""
        miss, cm, _ = tj.missing_columns(REAL_EXPORT_HEADER)
        self.assertEqual(miss, [])
        for k in tj.BLOCKING_KEYS:
            with self.subTest(key=k):
                self.assertIsNotNone(cm.get(k), '没认出 %s' % k)

    def test_simulated_data_columns_complete(self):
        """仓库自带模拟数据（按模范表生成）必须 15/15 —— 这是数据与代码的合同。"""
        rows = tj.read_csv_rows(P_SIM)
        miss, cm, _ = tj.missing_columns(list(rows[0].keys()))
        self.assertEqual(miss, [], '模拟数据缺列了：%s' % miss)
        for k in tj.REQUIRED_KEYS:
            with self.subTest(key=k):
                self.assertEqual(cm.get(k), tj.CSV_FIELDS[k])

    def test_short_names_resolve(self):
        """仓库 pull_real3.py 拍平的短名也要认。"""
        pairs = {'日期': 'date', '覆盖': 'reach', '展示': 'impressions', '链接点击': 'clicks',
                 '花费': 'spend', '加购': 'add_to_cart', '结账': 'initiate_checkout',
                 '购买': 'purchase', '支付信息': 'add_payment_info', '购买价值': 'purchase_value'}
        cm = tj.resolve_columns(list(pairs.keys()))
        for col, key in pairs.items():
            with self.subTest(col=col):
                self.assertEqual(cm.get(key), col)

    def test_purchase_not_swallowed_by_purchase_value(self):
        """别名必须精确命中：「购买」不能被「购买价值」吃掉（次数 ≠ 金额）。"""
        cm = tj.resolve_columns(['报告开始日期', '购买', '购买价值'])
        self.assertEqual(cm['purchase'], '购买')
        self.assertEqual(cm['purchase_value'], '购买价值')

    def test_cpc_column_is_optional(self):
        """cpc 列缺了不算缺 —— 30 维里的 CPC 是现算的，不用导出那一列。"""
        header = [c for c in REAL_EXPORT_HEADER if c != '单次链接点击费用 (USD)']
        miss, _, _ = tj.missing_columns(header)
        self.assertEqual(miss, [])
        self.assertIn('cpc', tj.OPTIONAL_KEYS)

    def test_missing_blocking_column_raises(self):
        """缺了会让特征失真的列：strict 时必须抛错，不许静默变 0。"""
        header = [c for c in REAL_EXPORT_HEADER if c != '购物次数']
        tmp = _write_csv(header)
        with self.assertRaises(tj.ColumnError):
            tj.load_csv_grouped(tmp, strict=True)
        groups = tj.load_csv_grouped(tmp, strict=False, quiet=True)
        self.assertTrue(groups, '放行时也要能读出分组')

    def test_column_used_for_covers_every_key(self):
        """COLUMN_USED_FOR 必须覆盖每个必需列 —— 防以后加列忘了写人话说明。"""
        for k in tj.REQUIRED_KEYS:
            with self.subTest(key=k):
                self.assertIn(k, tj.COLUMN_USED_FOR)

    def test_days_from_csv_rows_accepts_colmap(self):
        """days_from_csv_rows 支持外部传入 colmap，且与自动解析结果一致。"""
        rows = tj.read_csv_rows(P_SIM)
        cm = tj.resolve_columns(list(rows[0].keys()))
        a = tj.days_from_csv_rows(rows[:5], cm)
        b = tj.days_from_csv_rows(rows[:5])
        self.assertEqual([d['spend'] for d in a], [d['spend'] for d in b])
        self.assertEqual([d['date'] for d in a], [d['date'] for d in b])


class TestStoreUntouchedByRepo(unittest.TestCase):
    def test_store_path_is_repo_relative(self):
        """learning_store.jsonl 必须是运行时产物（落在仓库根、可被 .gitignore 排除）。"""
        self.assertEqual(os.path.basename(flywheel.P_STORE), 'learning_store.jsonl')
        self.assertEqual(os.path.dirname(flywheel.P_STORE), D)


# ==================== 多视图接入（metric_views）====================

# 五种视图的最小表头（只留签名列 + 日期 + 分组列），用于识别与合并用例。
H_CONV = ['报告开始日期', '广告系列名称', '展示次数', '链接点击量', '已花费金额 (USD)',
          '加入购物车次数', '购物次数', '购物转化价值']
H_CLICK = ['报告开始日期', '广告系列名称', '展示次数', '链接点击量', '点击量（全部）',
           '点击率（全部）', '落地页浏览量', '已花费金额 (USD)']
H_VIDEO = ['报告开始日期', '广告系列名称', '展示次数', '播放视频达 3 秒的次数',
           'ThruPlay 次数', '视频播放进度达 25% 的次数', '视频播放进度达 50% 的次数',
           '视频播放进度达 100% 的次数']
H_ENG = ['报告开始日期', '广告系列名称', '展示次数', '公共主页互动量', '帖子评论数',
         '帖子分享次数', 'Facebook 获赞数', 'Instagram 关注次数']
H_COST = ['报告开始日期', '广告系列名称', '单次成效费用', '结束日期']


def _write_export(header, rows, mtime=None):
    """写一份临时导出（BOM + 表头 + 数据行）。mtime 可指定，用于「更新的优先」用例。"""
    fd, p = tempfile.mkstemp(suffix='.csv')
    os.close(fd)
    with io.open(p, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


class TestViewDetection(unittest.TestCase):
    """视图识别 —— 决定「这份导出是哪一套指标」。"""

    def test_five_views_detected(self):
        for header, expect in ((H_CONV, 'conversion'), (H_CLICK, 'click'),
                               (H_VIDEO, 'video'), (H_ENG, 'engagement'),
                               (H_COST, 'cost')):
            with self.subTest(view=expect):
                name, hit = mv.detect_view(header)
                self.assertEqual(name, expect)
                self.assertGreater(hit, 0)

    def test_unknown_header(self):
        """一套签名列都不沾 → unknown，不许瞎猜。"""
        name, hit = mv.detect_view(['报告开始日期', '广告系列名称', '备注'])
        self.assertEqual(name, 'unknown')
        self.assertEqual(hit, 0)

    def test_signature_ignores_column_order_and_spaces(self):
        """表头指纹与列顺序/空格无关：同一套指标换个次序还是同一份。"""
        a = ['报告开始日期', '广告系列名称', '展示次数']
        b = ['展示次数', ' 广告系列名称', '报告开始日期']
        self.assertEqual(mv.header_sig(a), mv.header_sig(b))


class TestDedupe(unittest.TestCase):
    """过滤相同的 → 留下不同的。"""

    def test_same_header_keep_more_rows(self):
        small = _write_export(H_CONV, [['2026-01-01', '广告A'] + ['0'] * 6])
        big = _write_export(H_CONV, [['2026-01-0%d' % i, '广告A'] + ['0'] * 6 for i in (1, 2, 3)])
        exps = [mv.load_export(big), mv.load_export(small)]
        groups, chosen, dropped = mv.dedupe_exports(exps)
        self.assertEqual(len(groups), 1, '同表头必须归到一组')
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0].n, 3, '应当留数据行更多的那份')
        self.assertEqual([d.n for d in dropped], [1])

    def test_different_headers_all_kept(self):
        paths = [_write_export(H_CONV, [['2026-01-01', 'A'] + ['0'] * 6]),
                 _write_export(H_VIDEO, [['2026-01-01', 'A'] + ['0'] * 7])]
        _, chosen, dropped = mv.dedupe_exports([mv.load_export(p) for p in paths])
        self.assertEqual(len(chosen), 2)
        self.assertEqual(dropped, [])


class TestMerge(unittest.TestCase):
    """按 (日期, 广告) 拼宽表；冲突必须报出来。"""

    def test_two_views_merge_into_one_row(self):
        p1 = _write_export(H_CONV, [['2026-01-01', '广告A', '1000', '100', '20',
                                     '5', '2', '80']])
        p2 = _write_export(H_CLICK, [['2026-01-01', '广告A', '1000', '100', '1500',
                                      '6.0', '90', '20']])
        wide, _ = mv.merge_exports([mv.load_export(p1), mv.load_export(p2)])
        self.assertEqual(len(wide), 1, '同一天同一广告只能有一行')
        row = list(wide.values())[0]
        self.assertIn('购物转化价值', row, '转化视图的列要在')
        self.assertIn('点击量（全部）', row, '点击视图的列也要在')

    def test_conflict_is_reported_and_newest_wins(self):
        now = time.time()
        old = _write_export(H_CONV, [['2026-01-01', '广告A', '1000', '100', '7.56',
                                      '5', '2', '80']], mtime=now - 10000)
        new = _write_export(H_CONV, [['2026-01-01', '广告A', '1000', '100', '21.53',
                                      '5', '2', '80']], mtime=now)
        wide, rep = mv.merge_exports([mv.load_export(old), mv.load_export(new)])
        self.assertEqual(len(rep['conflicts']), 1, '同一格的两种取值必须留下记录')
        self.assertEqual(rep['conflicts'][0]['winner'], '21.53')
        row = list(wide.values())[0]
        self.assertEqual(row['已花费金额 (USD)'], '21.53', '更新的那份胜出')

    def test_identical_values_are_not_conflicts(self):
        now = time.time()
        a = _write_export(H_CONV, [['2026-01-01', '广告A', '1000', '100', '9.9',
                                    '5', '2', '80']], mtime=now - 100)
        b = _write_export(H_CONV, [['2026-01-01', '广告A', '1000', '100', '9.9',
                                    '5', '2', '80']], mtime=now)
        _, rep = mv.merge_exports([mv.load_export(a), mv.load_export(b)])
        self.assertEqual(rep['conflicts'], [], '值一样就不是冲突')


class TestExtFeatures(unittest.TestCase):
    """扩展特征：窗口必须配对、不可算必须是 None、比值 > 1 必须报警。"""

    def _one_ad(self, header, rows):
        p = _write_export(header, rows)
        wide, _ = mv.merge_exports([mv.load_export(p)])
        return mv.ext_report_by_ad(wide)['广告A']

    def test_paired_window_not_mixed(self):
        """分子只在第 2 天有值 → 分母也必须只取第 2 天，不能拿两天的点击去除。

        实测教训：互动系列的「落地页浏览量」只报 1 天，「链接点击量」报 4 天，
        不配对窗口会算出 0.0028（真实是 0.0317，差 11 倍）。
        """
        rep = self._one_ad(H_CLICK, [
            ['2026-01-01', '广告A', '1000', '100', '1200', '6.0', '', '10'],
            ['2026-01-02', '广告A', '2000', '200', '1400', '7.0', '50', '10'],
        ])
        self.assertAlmostEqual(rep['features']['落地页到达率'], 50.0 / 200.0, places=6)
        self.assertEqual(rep['days']['落地页到达率'], 1, '只用了 1 天，必须能看出来')

    def test_unavailable_is_none_not_zero(self):
        """缺列 → None（不是 0.0）。「这项没读到」和「这项是 0」不能混。"""
        rep = self._one_ad(H_CONV, [['2026-01-01', '广告A', '1000', '100', '20',
                                     '5', '2', '80']])
        self.assertIsNone(rep['features']['3秒播放率'])
        self.assertIsNone(rep['features']['全部点击倍数'])
        self.assertEqual(rep['days']['3秒播放率'], 0)

    def test_ratio_over_one_is_flagged(self):
        """v25 > video_3s（平台两个指标基底不同）→ 必须进 suspect 报警。"""
        rep = self._one_ad(H_VIDEO, [['2026-01-01', '广告A', '1000', '100',
                                      '10', '120', '60', '30']])
        self.assertGreater(rep['features']['视频留存25'], 1.0)
        self.assertIn('视频留存25', rep['suspect'])
        self.assertNotIn('视频留存50', rep['suspect'])

    def test_union_header_not_first_row(self):
        """视频列只存在于最后一天时，也要能算出来（不能只看第一行表头）。

        实测教训：视频/互动视图只覆盖最后 4 天，用 rows[0].keys() 会整块漏掉。
        """
        p1 = _write_export(H_CONV, [['2026-01-01', '广告A', '1000', '100', '20',
                                     '5', '2', '80'],
                                    ['2026-01-02', '广告A', '2000', '200', '30',
                                     '6', '3', '90']])
        # H_VIDEO 列序：日期, 名称, 展示次数, 3秒播放, ThruPlay, 25%, 50%, 100%
        p2 = _write_export(H_VIDEO, [['2026-01-02', '广告A', '2000', '200',
                                      '80', '150', '100', '40']])
        wide, _ = mv.merge_exports([mv.load_export(p1), mv.load_export(p2)])
        rep = mv.ext_report_by_ad(wide)['广告A']
        self.assertIsNotNone(rep['features']['3秒播放率'], '视频列在第二天，仍须算得出')
        self.assertAlmostEqual(rep['features']['3秒播放率'], 200.0 / 2000.0, places=6)
        self.assertAlmostEqual(rep['features']['视频完播率'], 40.0 / 200.0, places=6)
        self.assertEqual(rep['days']['3秒播放率'], 1)

    def test_ext_keys_disjoint_from_core(self):
        """扩展维度不许和核心 30 维重名 —— 免得以后混起来分不清谁是谁。"""
        self.assertEqual(len(tj.FEATURE_KEYS), 30)
        self.assertEqual(len(mv.EXT_FEATURE_KEYS), 13)
        self.assertEqual(set(tj.FEATURE_KEYS) & set(mv.EXT_FEATURE_KEYS), set())

    def test_core_feature_keeps_30(self):
        """接入多视图**不许**动核心 30 维（动了历史成绩全部作废）。"""
        self.assertEqual(len(tj.FEATURE_KEYS), 30)
        self.assertEqual(tj.FEATURE_KEYS[0], '花费')
        self.assertEqual(tj.FEATURE_KEYS[-1], '首单在第几天')
        # 合并真实/模拟数据都不该改变核心维度清单
        rows = tj.read_csv_rows(P_SIM)
        self.assertEqual(len(tj.FEATURE_KEYS), 30)
        self.assertGreater(len(rows), 0)


class TestBudgetAnomaly(unittest.TestCase):
    def test_text_budget_is_detected(self):
        """平台把文案写进「广告组预算」列时要报出来，否则读成 0 而无声。"""
        p = _write_export(['报告开始日期', '广告系列名称', '广告组预算', '广告组预算类型'],
                          [['2026-01-01', '广告A', '使用广告组预算', '0']])
        bad = mv.budget_anomaly([mv.load_export(p)])
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]['value'], '使用广告组预算')

    def test_numeric_budget_is_clean(self):
        p = _write_export(['报告开始日期', '广告系列名称', '广告组预算', '广告组预算类型'],
                          [['2026-01-01', '广告A', '40', '单日预算']])
        self.assertEqual(mv.budget_anomaly([mv.load_export(p)]), [])


class TestRealExports(unittest.TestCase):
    """真实账户的导出（若本机存在）—— 端到端跑一遍。

    ⚠️ 断言必须**由数据本身推出来**，不能写死份数 / 行数：
    用户随时会再导一份新指标的导出（2026-10-02 就多了一份带排名的**广告级**导出，
    直接从 6 份变 7 份、行数 177 变 354）。写死数字的断言会在那一刻变成**假警报**，
    而假警报会让人开始忽略测试 —— 比没有测试更糟。
    """

    # 真实导出的目录：默认「下载」文件夹，可用环境变量覆盖。
    # ⚠️ 不要写死带用户名的绝对路径 —— 这个文件是公开仓库的一部分。
    DL = os.environ.get('FB_EXPORT_DIR', os.path.expanduser(os.path.join('~', 'Downloads')))

    def _paths(self):
        import glob
        return sorted(glob.glob(os.path.join(self.DL, 'voglyn*.csv')))

    def test_every_export_is_classified_or_dropped(self):
        paths = self._paths()
        if len(paths) < 6:
            self.skipTest('本机没有那批真实导出')
        ov = mv.overview(paths=paths)
        self.assertEqual(len(ov['exports']), len(paths), '每份文件都该被扫描到')
        self.assertEqual(len(ov['chosen']) + len(ov['dropped']), len(paths),
                         '每份文件要么被选中，要么被去重丢掉，不能凭空消失')
        sigs = [e.sig for e in ov['chosen']]
        self.assertEqual(len(sigs), len(set(sigs)), '被选中的视图签名必须互不相同')
        # 五种视图的缺口清单必须算得出（哪怕某次导出恰好缺一种）
        self.assertIsInstance(ov['gap']['missing'], list)
        # 合并冲突**只允许报告、不允许静默**（广告级与系列级导出混在一起时确实会撞键）
        self.assertIsInstance(ov['merge']['conflicts'], list)

    def test_wide_table_beats_every_single_export(self):
        paths = self._paths()
        if len(paths) < 6:
            self.skipTest('本机没有那批真实导出')
        ov = mv.overview(paths=paths)
        widest = max(len(e.header) for e in ov['exports'])
        self.assertGreater(len(mv.wide_header(ov['wide'])), widest,
                           '合并后必须比任何单份都宽')
        most_rows = max(len(e.rows) for e in ov['exports'])
        self.assertGreaterEqual(len(ov['wide']), most_rows,
                                '合并后的行数不该少于最全的那一份')

    def test_ext_coverage_reflects_missing_days(self):
        paths = self._paths()
        if len(paths) < 6:
            self.skipTest('本机没有那批真实导出')
        ov = mv.overview(paths=paths)
        rep = ov['ext_report']
        # 视频/互动视图只覆盖 4 天 → 长袖/短袖那些广告的支撑天数必然少
        some_days = [d for r in rep.values() for d in r['days'].values() if d]
        self.assertTrue(some_days)
        self.assertTrue(any(d <= 4 for d in some_days),
                        '必须能看出有些比值只基于很少的天数')


class TestP0P1Columns(unittest.TestCase):
    """P0/P1：把「决定别的数字怎么读」的列读出来，并写进口径声明。

    这批字段（归因窗口 / 优化目标 / 排名 / 投放状态）**不参与任何计算**，
    价值全在「读数的前提」上 —— 实测两类真实误判就靠它们堵：

      · 归因设置：同一账户的两份导出窗口不同（一份「7天点击+1天浏览+1天互动观看」、
        另一份只有「7天点击+1天浏览」）→ 跨窗口比 ROAS 是错的
      · 成效指标：实测某系列优化的是 `actions:post_engagement`（互动），
        按「购买少」去关它是把靶子搞错了
    """

    HDR = ['报告开始日期', '广告名称', '广告投放', '归因设置', '成效指标',
           '已花费金额 (USD)', '展示次数', '覆盖人数', '链接点击量', '购物次数',
           '购物转化价值', '广告组预算', '广告组预算类型',
           '质量排名', '互动率排名', '转化率排名', '结束日期']

    def _write(self, rows, header=None):
        fd, path = tempfile.mkstemp(suffix='.csv')
        os.close(fd)
        with io.open(path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(header or self.HDR)
            w.writerows(rows)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _one_day(self, **over):
        r = {'报告开始日期': '2026-03-10', '广告名称': 'A1', '广告投放': 'not_delivering',
             '归因设置': '点击后 7 天内、浏览后 1 天内或互动观看后 1 天内',
             '成效指标': 'actions:offsite_conversion.fb_pixel_purchase',
             '已花费金额 (USD)': '20', '展示次数': '1000', '覆盖人数': '800',
             '链接点击量': '30', '购物次数': '2', '购物转化价值': '60',
             '广告组预算': '20', '广告组预算类型': '单日预算',
             '质量排名': '高于平均', '互动率排名': '平均', '转化率排名': '低于平均',
             '结束日期': '进行中'}
        r.update(over)
        return [r[k] for k in self.HDR]

    def test_sentinel_dash_is_none_not_a_value(self):
        """'-' 是「没有这项数据」，不是一种取值 —— 排名空 ≠ 排名差。"""
        self.assertIsNone(lo._text_or_none('-'))
        self.assertIsNone(lo._text_or_none(' -- '))
        self.assertIsNone(lo._text_or_none('N/A'))
        self.assertIsNone(lo._text_or_none(''))
        self.assertEqual(lo._text_or_none('高于平均'), '高于平均')

        rec = lo.load_file(self._write([self._one_day(质量排名='-',
                                                     互动率排名='-',
                                                     转化率排名='-')]))[0][0]
        self.assertNotIn('rankings', rec, '全是 "-" 时不该产出 rankings 字段')

    def test_new_fields_survive_into_receipt(self):
        rec = lo.load_file(self._write([self._one_day()]))[0][0]
        self.assertEqual(rec['status'], 'NOT_DELIVERING')
        self.assertIn('7 天', rec['attribution'])
        self.assertEqual(rec['optimization_event'],
                         'actions:offsite_conversion.fb_pixel_purchase')
        self.assertEqual(rec['rankings'],
                         {'quality': '高于平均', 'engagement': '平均',
                          'conversion': '低于平均'})
        self.assertEqual(rec['end_date'], '进行中')

    def test_ranking_column_present_but_empty_is_declared_as_gap(self):
        """导出了排名列但全是 '-' 时，必须明说「没有数据」，不能闭嘴。"""
        rows, meta = lo.load_file(self._write([self._one_day(质量排名='-',
                                                             互动率排名='-',
                                                             转化率排名='-')]))
        gaps = ' '.join(meta['data_gaps'])
        self.assertIn('排名', gaps)
        self.assertIn('不是排名差', gaps)

    def test_missing_ranking_column_is_also_declared(self):
        hdr = [h for h in self.HDR if '排名' not in h]
        idx = [self.HDR.index(h) for h in hdr]
        full = self._one_day()
        row = [full[i] for i in idx]
        rows, meta = lo.load_file(self._write([row], header=hdr))
        gaps = ' '.join(meta['data_gaps'])
        self.assertIn('排名', gaps)
        self.assertNotIn('rankings', rows[0])

    def test_snapshot_and_prompt_carry_the_caliber(self):
        rec = lo.load_file(self._write([self._one_day()]))[0][0]
        snap = eng.make_snapshot([rec])
        self.assertEqual(snap[0]['attribution'], rec['attribution'])
        self.assertEqual(snap[0]['optimization_event'], rec['optimization_event'])
        self.assertEqual(snap[0]['rankings'], rec['rankings'])
        p = eng.build_prompt(snap, {'lookback_days': 7}, window=('2026-03-10', '2026-03-10'))
        for clause in ('status 非 ACTIVE', 'attribution = 本批数据采用的归因窗口',
                       'optimization_event = 这个广告组优化', 'rankings（quality 质量',
                       '不等于「排名差」'):
            self.assertIn(clause, p, 'prompt 缺了口径声明：%s' % clause)

    def test_absent_caliber_fields_stay_absent(self):
        """没有这些列的文件（老导出）不能凭空长出口径字段来。"""
        hdr = [h for h in self.HDR
               if h not in ('归因设置', '成效指标', '质量排名', '互动率排名', '转化率排名')]
        idx = [self.HDR.index(h) for h in hdr]
        full = self._one_day()
        rows, meta = lo.load_file(self._write([[full[i] for i in idx]], header=hdr))
        rec = rows[0]
        for k in ('attribution', 'optimization_event', 'rankings'):
            self.assertNotIn(k, rec)
        gaps = ' '.join(meta['data_gaps'])
        self.assertIn('归因设置', gaps)

    def test_real_latest_export_if_present(self):
        """本机若有那份带排名的广告级导出，跑一遍真实数据。"""
        import glob
        dl = os.environ.get('FB_EXPORT_DIR', os.path.expanduser(os.path.join('~', 'Downloads')))
        cand = [p for p in sorted(glob.glob(os.path.join(dl, 'voglyn*')) )
                if '(6)' in os.path.basename(p) and p.lower().endswith('.csv')]
        if not cand:
            self.skipTest('本机没有那份广告级导出')
        rows, meta = lo.load_file(cand[0])
        self.assertTrue(all(r.get('attribution') for r in rows),
                        '真实导出里归因设置应当每一行都能读到')


class TestP1bSignals(unittest.TestCase):
    """P1b：广告组粒度 + 视频平均播放时长 + 重复点击率。

    这批和 P0/P1 不是一类东西：P0/P1 是「决定别的数字怎么读」的前提，
    P1b 是**两个从任何现有指标都推不出来的观测**：

      · video_avg_watch_sec —— 观众平均看了几秒。3 秒播放数高但时长只有 1~2 秒，
        说明素材只赢在开头；这个判断用 CTR/ROAS 怎么算都算不出来。
      · repeat_click_ratio —— 非独立点击数占全部点击的比例。高 = 少数人反复点
        （落地页/价格/信任问题），低 = 很多人在点（素材/受众问题）。同样推不出来。

    真实触发这两个 bug 的那份导出有两个陷阱，都写进用例里了：
      ① 时长列格式混用：零值写 '00:00:00'，非零值写裸秒数 '2'；
      ② 费用四舍五入到 6 位小数 → 11.37/0.5685 = 20.0004，除完带尾差，
         直接算 1-x 会得到负数并把「完全不重复」整条丢掉。
    """

    HDR = ['报告开始日期', '广告组名称', '广告组投放', '已花费金额 (USD)', '展示次数',
           '覆盖人数', '链接点击量', '购物次数', '购物转化价值', '广告组预算',
           '广告组预算类型', '视频平均播放时长', '单次链接点击费用 - 独立用户 (USD)']

    def _write(self, rows, header=None):
        fd, path = tempfile.mkstemp(suffix='.csv')
        os.close(fd)
        with io.open(path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(header or self.HDR)
            w.writerows(rows)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _row(self, over=None):
        """造一行数据。over 用 dict 传 —— 列名带空格和括号，不能当关键字参数名。"""
        r = {'报告开始日期': '2026-03-10', '广告组名称': '组A', '广告组投放': 'not_delivering',
             '已花费金额 (USD)': '11.37', '展示次数': '318', '覆盖人数': '294',
             '链接点击量': '22', '购物次数': '1', '购物转化价值': '63.95',
             '广告组预算': '20', '广告组预算类型': '单日预算',
             '视频平均播放时长': '4', '单次链接点击费用 - 独立用户 (USD)': '0.5685'}
        r.update(over or {})
        return [r[k] for k in self.HDR]

    # ---------- 时长解析 ----------
    def test_secs_parses_both_formats_meta_uses(self):
        self.assertEqual(lo._secs('4'), 4.0)            # 裸秒数（非零值的真实格式）
        self.assertEqual(lo._secs('00:00:00'), 0.0)     # 零值的真实格式
        self.assertEqual(lo._secs('00:01:30'), 90.0)
        self.assertEqual(lo._secs('1:30'), 90.0)        # 缺小时位
        self.assertEqual(lo._secs('00:00:02.5'), 2.5)

    def test_secs_does_not_read_colon_as_decimal(self):
        """反向断言：'00:01:30' 是 90 秒，绝不能被读成 130（把冒号当小数点）。"""
        self.assertNotEqual(lo._secs('00:01:30'), 130.0)

    def test_secs_empty_sentinels_are_none(self):
        for v in ('', '-', '--', 'n/a', None):
            self.assertIsNone(lo._secs(v), '输入 %r 应当读成 None' % (v,))

    # ---------- 广告组粒度 ----------
    def test_adset_grain_is_accepted(self):
        """以前按「广告组」层级导出的文件会被结构校验直接拒掉（找不到标识列）。"""
        rows, meta = lo.load_file(self._write([self._row()]))
        self.assertEqual(meta.get('grain'), 'adset')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['name'], '组A')

    def test_adset_level_status_column_is_read(self):
        """广告组层级导出里状态列叫「广告组投放」。

        以前别名表里没有它 → status 落进 (None or 'ACTIVE') → 一条**没在投**的广告
        被报成 ACTIVE，而 prompt 明写「status 非 ACTIVE 不得加预算」——
        这等于把最该拦住的那条规则给绕过去了。
        """
        rows, _ = lo.load_file(self._write([self._row({'广告组投放': 'not_delivering'})]))
        self.assertEqual(rows[0]['status'], 'NOT_DELIVERING')

    def test_campaign_grain_not_stolen_by_adset_branch(self):
        """只有系列名的老文件必须仍然走 campaign 粒度（判断顺序回归）。"""
        hdr = ['报告开始日期', '广告系列名称', '已花费金额 (USD)', '展示次数', '覆盖人数',
               '链接点击量', '购物次数', '购物转化价值']
        vals = {'报告开始日期': '2026-03-10', '广告系列名称': '系列A',
                '已花费金额 (USD)': '20', '展示次数': '1000', '覆盖人数': '800',
                '链接点击量': '30', '购物次数': '2', '购物转化价值': '60'}
        rows, meta = lo.load_file(self._write([[vals[c] for c in hdr]], header=hdr))
        self.assertEqual(meta.get('grain'), 'campaign')
        self.assertEqual(rows[0]['name'], '系列A')

    # ---------- 重复点击率 ----------
    def test_repeat_click_ratio_computed(self):
        """11.37 花费 / 22 次点击 / 独立点击单价 0.5685 → 独立用户 20 → 重复率 1-20/22。"""
        rows, _ = lo.load_file(self._write([self._row()]))
        self.assertAlmostEqual(rows[0]['repeat_click_ratio'], 0.0909, places=3)

    def test_repeat_click_ratio_zero_survives_rounding(self):
        """11 次点击来自 11 个人 → 重复率 0。

        11.37/0.923636 = 11.00002（CSV 费用保留 6 位小数的尾差），
        不做容差取整就会得到 -0.0000018，被守卫整条丢掉 —— 真实值明明是 0。
        """
        rows, _ = lo.load_file(self._write([self._row({
            '已花费金额 (USD)': '10.16', '链接点击量': '11',
            '单次链接点击费用 - 独立用户 (USD)': '0.923636'})]))
        self.assertEqual(rows[0].get('repeat_click_ratio'), 0.0)
        self.assertEqual(rows[0].get('repeat_click_days'), 1)

    def test_repeat_click_ratio_absent_when_column_missing(self):
        """没有这一列 → 字段整个不出现，绝不能填 0 冒充「观测到的零」。"""
        hdr = [c for c in self.HDR if c != '单次链接点击费用 - 独立用户 (USD)']
        vals = dict(zip(self.HDR, self._row()))
        rows, meta = lo.load_file(self._write([[vals.get(c, '') for c in hdr]], header=hdr))
        self.assertNotIn('repeat_click_ratio', rows[0])
        self.assertTrue(any('独立用户点击费用' in g for g in (meta.get('data_gaps') or [])),
                        '缺列必须在 data_gaps 里明说')

    def test_unknown_status_value_surfaces_in_data_gaps(self):
        """🔴 2026-10-04 验收第 4 问：换状态写法必须当场暴露，不能再无声失灵。"""
        rows, meta = lo.load_file(self._write([self._row({'广告组投放': '投放中'})]))
        self.assertFalse(any('状态列' in g for g in meta['data_gaps']),
                         '认识的词不该告警：%s' % meta['data_gaps'])
        rows, meta = lo.load_file(self._write([self._row({'广告组投放': 'SomeNewStatus'})]))
        self.assertTrue(any('状态列' in g and 'SOMENEWSTATUS' in g
                            for g in meta['data_gaps']),
                        '未知状态必须在 data_gaps 里亮出来：%s' % meta['data_gaps'])

    def test_repeat_click_ratio_not_computed_across_windows(self):
        """分子分母必须同一天。跨窗口硬凑 = 之前踩过的 11 倍误差。"""
        rows, _ = lo.load_file(self._write([
            self._row({'报告开始日期': '2026-03-10', '已花费金额 (USD)': '20',
                       '链接点击量': '10', '单次链接点击费用 - 独立用户 (USD)': '1.0'}),
            self._row({'报告开始日期': '2026-03-11', '已花费金额 (USD)': '0',
                       '链接点击量': '0', '单次链接点击费用 - 独立用户 (USD)': '0.5'}),
        ]))
        # 第 1 天独立点击 20 > 全部点击 10 → 口径不自洽 → 两天都不该计入
        self.assertNotIn('repeat_click_ratio', rows[0])

    # ---------- 观看时长 ----------
    def test_watch_seconds_ignore_zero_spend_days(self):
        """零投放的天写 '00:00:00'，计进去会把均值稀释成假的「平均 0.07 秒」。"""
        rows, _ = lo.load_file(self._write([
            self._row({'报告开始日期': '2026-03-10', '视频平均播放时长': '4'}),
            self._row({'报告开始日期': '2026-03-11', '已花费金额 (USD)': '0',
                       '展示次数': '0', '覆盖人数': '0', '链接点击量': '0',
                       '购物次数': '0', '购物转化价值': '0',
                       '视频平均播放时长': '00:00:00'}),
        ]))
        self.assertEqual(rows[0]['video_avg_watch_sec'], 4.0)
        self.assertEqual(rows[0]['video_watch_days'], 1,
                         '支撑天数必须是 1（只有 03-10 真投放），不是 2')

    def test_watch_fields_absent_when_all_zero(self):
        hdr = [c for c in self.HDR if c != '视频平均播放时长']
        vals = dict(zip(self.HDR, self._row()))
        rows, _ = lo.load_file(self._write([[vals.get(c, '') for c in hdr]], header=hdr))
        self.assertNotIn('video_avg_watch_sec', rows[0])

    def test_status_takes_latest_day_not_first(self):
        """投放状态是**时点事实** —— 必须取窗口里最后一天，不是第一天。

        踩过的坑：模拟数据里 A7 前 3 天 ACTIVE、后 2 天 PAUSED，
        加载器取「第一个非空值」⇒ 一条**已被暂停**的广告被报成 ACTIVE。
        而 prompt 明写「status 非 ACTIVE 不得给 increase_budget」
        ⇒ 等于把最该拦住的那条规则绕过去，还顺手给一条停投的广告建议加预算。
        """
        rows, _ = lo.load_file(self._write([
            self._row({'报告开始日期': '2026-04-01', '广告组投放': 'ACTIVE'}),
            self._row({'报告开始日期': '2026-04-02', '广告组投放': 'ACTIVE'}),
            self._row({'报告开始日期': '2026-04-03', '广告组投放': 'PAUSED'}),
        ]))
        self.assertEqual(rows[0]['status'], 'PAUSED')

    def test_status_empty_cell_does_not_fall_back_to_active(self):
        """列在、但某天的值是空 —— 不能把状态「顶」回 ACTIVE。

        区分两种情况：
          · **整列都没有** → 真的没有状态信息，回退 ACTIVE 是既定行为（且要写进 data_gaps）
          · **列在、某个格子空** → 那是「这天没填」，应沿用最近一次真实取值
        """
        v1 = dict(zip(self.HDR, self._row()))
        v1['报告开始日期'] = '2026-04-01'
        v1['广告组投放'] = 'PAUSED'
        v2 = dict(zip(self.HDR, self._row()))
        v2['报告开始日期'] = '2026-04-02'
        v2['广告组投放'] = ''          # 空值哨兵，不是取值
        v3 = dict(zip(self.HDR, self._row()))
        v3['报告开始日期'] = '2026-04-03'
        v3['广告组投放'] = ''          # 末天空着，也不能丢掉前面读到的 PAUSED
        rows, _ = lo.load_file(self._write([[d[k] for k in self.HDR] for d in (v1, v2, v3)]))
        self.assertEqual(rows[0]['status'], 'PAUSED')

    # ---------- prompt ----------
    def test_prompt_declares_how_to_read_new_signals(self):
        rows, _ = lo.load_file(self._write([self._row()]))
        p = eng.build_prompt(rows, {}, window=None)
        for needle in ('video_avg_watch_sec', 'video_watch_days',
                       'repeat_click_ratio', 'repeat_click_days'):
            self.assertIn(needle, p)
        # ⚠️ 这两条断言在 2026-10-03 被**加强**过：原措辞是「不得因此给 decrease_budget」——
        #   那个措辞有个大洞：通篇**没禁 pause**，而三轮盲测全都判了 pause。
        #   等于只禁了一半，另一半形同虚设。详见 TestLossAttributionPrompt 的
        #   test_rules_have_no_escape_hatch。
        self.assertIn('都不许给', p)
        self.assertIn('落地页、价格、信任这一环查过了吗', p)
        self.assertIn('净额为负不构成关停理由', p)

    def test_signal_flags_switch(self):
        """信号旗开关：只影响「点名那一段」，不动无条件禁令。

        2026-10-03 新增。条件 B 实测（同数据、同快照，仅关旗）：
        两轮仍 8/8、T-05/T-06 翻正 2/2 ⇒ 旗是冗余保险，不是主力。
        所以关旗后的 prompt 必须**仍含**禁令原文，否则这个开关就成了后门。
        """
        snap = [{'id': 'x', 'name': 'x', 'repeat_click_ratio': 0.45}]
        win = ('2026-04-01', '2026-04-05')
        on = eng.build_prompt(snap, {}, window=win)
        off = eng.build_prompt(snap, {'enable_signal_flags': False}, window=win)
        self.assertIn('有强信号', on)              # 默认开
        self.assertNotIn('有强信号', off)          # config 关得掉
        for needle in ('净额为负不构成关停理由', '必须先回答一个问题'):
            self.assertIn(needle, off)             # 禁令仍在：开关只是少一段点名

    # ---------- 真实数据 ----------
    def test_real_adset_export(self):
        """本机若有那份广告组级导出，跑一遍真实数据。"""
        import glob
        dl = os.environ.get('FB_EXPORT_DIR', os.path.expanduser(os.path.join('~', 'Downloads')))
        cand = [p for p in sorted(glob.glob(os.path.join(dl, 'voglyn*')))
                if '(7)' in os.path.basename(p) and p.lower().endswith('.csv')]
        if not cand:
            self.skipTest('本机没有那份广告组级导出')
        rows, meta = lo.load_file(cand[0])
        self.assertEqual(meta.get('grain'), 'adset')
        self.assertTrue(all(r['status'] == 'NOT_DELIVERING' for r in rows),
                        '真实导出里广告全是 not_delivering（没在投）')
        # 真实文件里只有 2026-03-10 一天有花费 → 支撑天数必须是 1，不能是 59
        for r in rows:
            if 'video_watch_days' in r:
                self.assertLessEqual(r['video_watch_days'], 2)
            if 'repeat_click_days' in r:
                self.assertLessEqual(r['repeat_click_days'], 2)


class TestStopPrecondition(unittest.TestCase):
    """护栏止损线的**前置条件**（2026-10-03 双盲实测后加）。

    背景：止损线只看 `net = 购买价值 − 花费`，不看口径列、不看样本量、不看「亏在哪一环」。
    双盲实测里它把两条本不该关的广告一刀切停（优化加购的那条 + 只点 50 次就零单的那条）。

    ⚠️ 护栏**只作用于「AI 没判停但净额为负」这一个方向**；AI 自己判了 pause 的它本来就不动。
    所以这些前置条件不会「放过该关的广告」，它只阻止「护栏替 AI 做它不该做的决定」。
    """

    BUY = 'actions:offsite_conversion.fb_pixel_purchase'
    CART = 'actions:offsite_conversion.fb_pixel_add_to_cart'

    def _row(self, **over):
        r = {'id': 'ad1', 'name': 'X', 'status': 'ACTIVE', 'optimization_event': self.BUY,
             'clicks': 500, 'spend': 120.0, 'purchase_value': 0.0}
        r.update(over)
        nv = {'net': round(float(r['purchase_value']) - float(r['spend']), 2),
              'spend': r['spend'], 'revenue': r['purchase_value'],
              'fill_rate': 1.0, 'freq_first': 2.0, 'freq_last': 2.4, 'days': 5}
        # ⚠️ net_view 必须挂在 row 上：护栏是 `row.get('net_view')` 读的，
        #    返回一个独立的字典会让它走 no_net 提前返回 —— 看着像「护栏没生效」。
        r['net_view'] = nv
        r['daily_spend'] = []
        return r, nv

    def _hit(self, **over):
        r, nv = self._row(**over)
        blocked, why = eng._stop_precondition(r, nv, {})
        return blocked, why

    # ---- 命中：四条实测误杀 ----
    def test_blocks_when_not_active(self):
        b, why = self._hit(status='PAUSED')
        self.assertTrue(b)
        self.assertIn('不在投', why)

    def test_blocks_when_optimization_is_not_purchase(self):
        b, why = self._hit(optimization_event=self.CART)
        self.assertTrue(b)
        self.assertIn('不是 purchase', why)

    def test_low_clicks_alone_never_blocks_stoploss(self):
        """🔴 2026-10-03 用户纠正：「没单就是没单，点击再多也得关」。

        原来的 `STOP_MIN_CLICKS = 100`（「点击少 ⇒ 零单是样本不足 ⇒ 不止损」）是错的：
        它把**统计判断**（「这个样本量下 0 单不算反常」）当成了**不花钱的理由**。
        天天满额投放、净亏、零单的广告，钱在持续流出 ⇒ **必须照关**，哪怕只点了 30 次。
        """
        r, nv = self._row(clicks=30, spend=120.0)
        blocked, why = eng._stop_precondition(r, nv, {})
        self.assertFalse(blocked, '满额投放 + 净亏 + 零单，点击再少也要关；却被拦：%s' % why)

    def test_extremely_low_clicks_still_force_paused(self):
        """端到端：点击 8 次的满额广告，护栏照样 forced_pause（老门槛会放过它）。"""
        snap = mk_snapshot('1', net=-25.0, spend=25.0, rev=0.0, fill=1.0, freq=3.0, days=5)
        snap['clicks'] = 8
        sugg = [{'campaign_id': '1', 'action': 'increase_budget',
                 'budget_change_pct': 20, 'reason': '模型想加预算'}]
        out, stats = eng.enforce_risk_guardrails(sugg, [snap], {})
        self.assertEqual(out[0]['action'], 'pause', out[0].get('reason'))
        self.assertEqual(out[0]['guardrail'], 'forced_pause')
        self.assertEqual(stats['forced_pause'], 1)

    def test_blocks_only_when_underspend_AND_low_freq(self):
        """真正该给 observe 的形态是**探索期**：预算没花出去 **且** 受众还没看腻。"""
        r, nv = self._row(clicks=171, spend=67.0)
        nv['fill_rate'] = 0.05
        nv['freq_last'] = 1.69
        blocked, why = eng._stop_precondition(r, nv, {})
        self.assertTrue(blocked)
        self.assertIn('探索期', why)

    def test_underspend_alone_is_not_enough_to_block(self):
        """顶格率低但频次已经上来了 = 探索期结束，只是花钱效率差 ⇒ 照关。"""
        r, nv = self._row(clicks=800, spend=300.0)
        nv['fill_rate'] = 0.05
        nv['freq_last'] = 3.2
        blocked, why = eng._stop_precondition(r, nv, {})
        self.assertFalse(blocked, '频次已过 2.5，探索期结束，不该拦：%s' % why)

    def test_clicks_floor_constant_is_gone(self):
        """钉住删除：不能让人后来又把 clicks 门槛加回来当防误杀。"""
        self.assertFalse(hasattr(eng, 'STOP_MIN_CLICKS'),
                         'STOP_MIN_CLICKS 已删除 —— 点击数不是止损判据')
        self.assertTrue(hasattr(eng, 'STOP_MIN_SPEND_RATE'))
        self.assertTrue(hasattr(eng, 'STOP_EXPLORE_FREQ_MAX'))

    def test_loss_size_gate_is_deliberately_disabled(self):
        """否决记录钉成用例：**不能**让人后来又把它加回来。

        「亏 50 美元就关」是噪音；但「亏 50 美元就永远不管」是更大的错 ——
        每天只花 $5、每天亏 $3 的广告会因此永远不被止损线关掉，小预算持续失血。
        """
        self.assertIsNone(eng.STOP_MIN_LOSS_ABS)
        r, nv = self._row(spend=20.0)
        blocked, why = eng._stop_precondition(r, nv, {})
        self.assertFalse(blocked, '小额亏损仍应照常止损：%s' % why)

    def test_blocks_when_watch_time_too_short(self):
        r, nv = self._row(spend=600.0)
        r['video_avg_watch_sec'] = 2.1
        blocked, why = eng._stop_precondition(r, nv, {})
        self.assertTrue(blocked)
        self.assertIn('素材', why)

    def test_blocks_when_repeat_click_high(self):
        r, nv = self._row(spend=500.0)
        r['repeat_click_ratio'] = 0.45
        blocked, why = eng._stop_precondition(r, nv, {})
        self.assertTrue(blocked)
        self.assertIn('反复点', why)

    # ---- 不命中：该关的必须照关 ----
    def test_does_not_block_a_genuinely_dead_ad(self):
        """T-03 那种：ACTIVE + 优化购买 + 415 点击 + 净亏 750 + 无强信号 ⇒ 必须照关。"""
        r, nv = self._row(spend=750.0, clicks=415)
        blocked, why = eng._stop_precondition(r, nv, {})
        self.assertFalse(blocked, '不该拦：%s' % why)

    def test_guardrail_still_forces_pause_when_nothing_blocks(self):
        r, nv = self._row(spend=750.0)
        snaps = [r]
        sugg = [{'campaign_id': 'ad1', 'action': 'observe', 'reason': 'x'}]
        out, stats = eng.enforce_risk_guardrails(sugg, snaps, {})
        self.assertEqual(out[0]['action'], 'pause')
        self.assertEqual(stats['forced_pause'], 1)

    def test_guardrail_does_not_override_paused_ad(self):
        """AI 自己判了 pause 的，护栏本来就不动；前置条件也不该把它放回来。"""
        r, nv = self._row(spend=50.0)
        sugg = [{'campaign_id': 'ad1', 'action': 'pause', 'reason': 'x'}]
        out, stats = eng.enforce_risk_guardrails(sugg, [r], {})
        self.assertEqual(out[0]['action'], 'pause')
        self.assertEqual(stats['forced_pause'], 0)
        self.assertEqual(stats['blocked_by_precondition'], 0)

    def test_precondition_downgrades_to_observe_with_reason(self):
        r, nv = self._row(optimization_event=self.CART, spend=600.0)
        sugg = [{'campaign_id': 'ad1', 'action': 'increase_budget', 'reason': 'x'}]
        out, stats = eng.enforce_risk_guardrails(sugg, [r], {})
        self.assertEqual(out[0]['action'], 'observe')
        self.assertEqual(out[0]['guardrail'], 'blocked_by_precondition')
        self.assertEqual(stats['forced_pause'], 0)
        self.assertEqual(stats['blocked_by_precondition'], 1)
        self.assertIn('护栏不替你做这个决定', out[0]['reason'])


class TestNomination(unittest.TestCase):
    """加预算提名：饱和线替代拟合值 + 停投对象不提名。"""

    BUY = 'actions:offsite_conversion.fb_pixel_purchase'

    def _snap(self, **over):
        r = {'id': 'ad1', 'name': 'X', 'status': 'ACTIVE', 'optimization_event': self.BUY,
             'clicks': 2200, 'net_view': {
                 'net': 2225.0, 'spend': 750.0, 'revenue': 2975.0,
                 'fill_rate': 1.0, 'freq_first': 1.9, 'freq_last': 2.4, 'days': 5}}
        r.update(over)
        return r

    def test_freq_2_4_now_nominates(self):
        """实测：freq 2.4（远未饱和、加预算后 ROAS 反升）在旧的 1.35 门槛下提名数为 0。"""
        self.assertEqual(eng.NOMINATE_FREQ_SATURATED, 3.5)
        sugg = [{'campaign_id': 'ad1', 'action': 'observe', 'reason': 'x'}]
        out, stats = eng.enforce_risk_guardrails(sugg, [self._snap()], {})
        self.assertEqual(stats['nominated'], 1)
        self.assertEqual(out[0]['nomination']['freq_last'], 2.4)

    def test_near_saturated_freq_does_not_nominate(self):
        sugg = [{'campaign_id': 'ad1', 'action': 'observe', 'reason': 'x'}]
        snap = self._snap()
        snap['net_view']['freq_last'] = 4.2
        out, stats = eng.enforce_risk_guardrails(sugg, [snap], {})
        self.assertEqual(stats['nominated'], 0)

    def test_rapidly_rising_freq_does_not_nominate(self):
        """频次不高但在快速堆高（末−首 = 1.2）⇒ 放量空间正在关闭，不该提名。"""
        sugg = [{'campaign_id': 'ad1', 'action': 'observe', 'reason': 'x'}]
        snap = self._snap()
        snap['net_view']['freq_first'] = 1.2
        snap['net_view']['freq_last'] = 2.4      # 绝对值不饱和，但增幅 1.2 > 0.5
        out, stats = eng.enforce_risk_guardrails(sugg, [snap], {})
        self.assertEqual(stats['nominated'], 0)

    def test_paused_ad_is_never_nominated(self):
        """重放当场抓到的 bug：PAUSED 的广告被判 observe，净额为正 + 顶格 + 频次不饱和
        ⇒ 提名了。「给一条已经停投的广告建议加预算」是荒谬的。"""
        sugg = [{'campaign_id': 'ad1', 'action': 'observe', 'reason': 'x'}]
        out, stats = eng.enforce_risk_guardrails(sugg, [self._snap(status='PAUSED')], {})
        self.assertEqual(stats['nominated'], 0)
        self.assertNotIn('nomination', out[0])

    def test_legacy_config_still_wins(self):
        """老配置里写了 nominate_freq_max 就以它为准（别让升级悄悄改掉别人的阈值）。"""
        sugg = [{'campaign_id': 'ad1', 'action': 'observe', 'reason': 'x'}]
        out, stats = eng.enforce_risk_guardrails(sugg, [self._snap()],
                                                {'nominate_freq_max': 1.35})
        self.assertEqual(stats['nominated'], 0, '旧阈值 1.35 下 freq 2.4 不该提名')


class TestLossAttributionPrompt(unittest.TestCase):
    """prompt 里那两条新东西：亏损归因规则 + 按快照动态点名强信号。"""

    def _snap(self, name='X', **over):
        r = {'id': 'ad1', 'name': name, 'status': 'ACTIVE',
             'spend': 100.0, 'clicks': 100, 'purchase': 0, 'daily_spend': []}
        r.update(over)
        return r

    def test_prompt_has_loss_attribution_rule(self):
        p = eng.build_prompt([self._snap()], {}, window=('2026-04-01', '2026-04-05'))
        self.assertIn('判「亏在哪」', p)
        self.assertIn('买不到有效流量', p)
        self.assertIn('流量买到了但接不住', p)

    def test_prompt_says_low_clicks_is_not_a_stay_open_reason(self):
        """🔴 2026-10-03 用户纠正后重写。

        prompt 原来写「几天零单本身不是关停依据，点击量不够时零单会自然发生」——
        这句话会被读成「点击少 ⇒ 不关」，直接制造漏放（大样本实测漏放 50%）。
        现在必须写清：判据是**有没有在正常花钱**（顶格率 + 频次），不是点击数；
        并且明确「满额投放 + 净亏 + 零单 ⇒ 该关，哪怕只点了 30 次」。
        """
        p = eng.build_prompt([self._snap()], {}, window=('2026-04-01', '2026-04-05'))
        self.assertIn('零单就是零单', p)
        self.assertIn('有没有在正常花钱', p)
        self.assertIn('探索期', p)
        self.assertIn('该关', p)
        # 旧措辞不能残留 —— 它是漏放的直接来源
        self.assertNotIn('几天零单', p)
        self.assertNotIn('点击量不够时零单会自然发生', p)

    def test_flag_section_names_the_high_repeat_click_ad(self):
        """信号埋在字段说明里没用（双盲两轮都没读它）⇒ 命中阈值必须按名字单独点名。"""
        snaps = [self._snap('甲', repeat_click_ratio=0.45),
                 self._snap('乙', repeat_click_ratio=0.02)]
        p = eng.build_prompt(snaps, {}, window=('2026-04-01', '2026-04-05'))
        seg = eng._flag_section(snaps)
        self.assertIn('甲', seg)
        self.assertNotIn('乙', seg)
        self.assertIn('强信号', p)

    def test_flag_section_names_short_watch_time_ad(self):
        snaps = [self._snap('丙', video_avg_watch_sec=2.0)]
        seg = eng._flag_section(snaps)
        self.assertIn('丙', seg)
        self.assertIn('素材', seg)

    def test_flag_section_empty_when_no_signal(self):
        snaps = [self._snap('丁')]
        self.assertEqual(eng._flag_section(snaps), '')

    def test_rules_have_no_escape_hatch(self):
        """🔴 钉死一条教训：**规则里留「结合 X 再定」这种尾巴 = 没写禁令。**

        2026-10-03 双盲复盘：重复点击那条规则原文是
            「不得**仅凭它**给 pause，但**必须结合 net_view.net 的正负再定**」
        —— 而那两条广告的 net 恰好都是负的，**规则字面上就允许 pause**。
        于是三轮盲测全都「正确地遵守了规则」却仍然误杀。
        观看时长那条更松：通篇只禁了 decrease_budget，**pause 从来没被禁**。

        所以这两条现在必须是**无条件的禁令**，且不能出现「再定」「酌情」这类退路。
        """
        p = eng.build_prompt([self._snap()], {}, window=('2026-04-01', '2026-04-05'))
        i = p.find('repeat_click_ratio = 非独立点击数')
        j = p.find('判「亏在哪」')
        seg = p[i:j]
        self.assertIn('落地页、价格、信任这一环查过了吗', seg)
        self.assertIn('净额为负不构成关停理由', seg)
        self.assertNotIn('必须结合 net_view.net 的正负再定', seg,
                         '旧版那句是逃生口，必须已经删掉')
        # 观看时长那条必须**显式禁 pause**（原来只禁了 decrease_budget）
        k = p.find('video_avg_watch_sec = 观众平均看了几秒')
        seg2 = p[k:i]
        self.assertIn('都不许给', seg2)
        self.assertIn('pause', seg2)

    def test_pause_is_framed_as_irreversible(self):
        """对冲「先停损」这个本能：把关停的代价算出来，而不是只喊「别关」。"""
        p = eng.build_prompt([self._snap()], {}, window=('2026-04-01', '2026-04-05'))
        self.assertIn('pause 是不可逆动作', p)
        self.assertIn('先停损再修', p)
        self.assertIn('已经试过什么、为什么不管用', p)


class TestTargetRoas(unittest.TestCase):
    """目标值判据（2026-10-03 用户提出）。

    起因是实测出的真漏洞：`net > 0 + 顶格 + 频次不饱和` 会被提名加预算，
    可那批广告 ROAS 只有 1.17~1.56 —— 按 50% 毛利全都在亏，加钱只是亏更快。
    根因：`net > 0` 等价于「ROAS >= 1」，而广告费的真实盈亏线是 **ROAS = 1/毛利率**。
    """

    BUY = 'actions:offsite_conversion.fb_pixel_purchase'
    CART = 'actions:offsite_conversion.fb_pixel_add_to_cart'

    def _row(self, spend=540.0, rev=842.0, orders=24, opt=None, **over):
        """默认造一条：净额为正、ROAS≈1.56、天天顶格、频次 2.4（= 那条真漏洞）。"""
        r = {'id': 'a1', 'name': 'X', 'status': 'ACTIVE',
             'optimization_event': opt or self.BUY,
             'spend': spend, 'purchase_value': rev, 'purchase': orders,
             'clicks': 800, 'cost_per_purchase': spend / max(orders, 1)}
        r.update(over)
        r['net_view'] = {'net': round(rev - spend, 2), 'spend': spend, 'revenue': rev,
                         'roas': round(rev / spend, 2), 'fill_rate': 1.0,
                         'freq_first': 2.2, 'freq_last': 2.4, 'days': 5}
        return r

    def _sugg(self, act='observe'):
        return [{'campaign_id': 'a1', 'action': act, 'budget_change_pct': 0,
                 'reason': '模型原判'}]

    # ---------- 读取：未填必须真的不启用 ----------
    def test_unset_target_roas_means_no_gate(self):
        """没填目标 ROAS ⇒ 资格线不生效（不能悄悄用默认值，那等于代码替用户做决定）。"""
        r = self._row()
        out, st = eng.enforce_risk_guardrails(self._sugg(), [r], {'target_roas': None})
        self.assertIsNone(st.get('blocked_by_target_roas'),
                          '未填时不该拦提名：%s' % out[0].get('reason', ''))

    def test_defaults_are_all_disabled(self):
        """🔴 2026-10-03 实测抓到的文档/代码不一致。

        原来 ，于是**不填也生效**（已经在拦提名），
        而文档写的是「没填不启用」—— 代码悄悄替用户选了一个数。
        这正是本项目反复吃亏的那类错（拟合值当业务值）。

        ⇒ 三个目标值的**代码缺省必须都是 None**。要启用就显式填。
        """
        self.assertIsNone(eng.TARGET_ROAS, '默认必须是不启用')
        self.assertIsNone(eng.BREAKEVEN_ROAS)
        self.assertIsNone(eng.TARGET_CPA)
        # 且空配置时三个读取函数都得返回 None
        self.assertIsNone(eng._target_roas({}))
        self.assertIsNone(eng._breakeven_roas({}))
        self.assertIsNone(eng._target_cpa({}))

    def test_empty_config_changes_nothing(self):
        """空配置 ⇒ 行为与「加目标值之前」完全一致（不能偷偷变严）。"""
        r = self._row()          # ROAS 1.56、净额为正
        out, st = eng.enforce_risk_guardrails(self._sugg(), [r], {})
        self.assertNotEqual(st.get('blocked_by_target_roas'), 1,
                            '空配置不该拦提名：%s' % out[0].get('reason'))
        self.assertNotEqual(out[0].get('guardrail'), 'forced_pause',
                            '空配置不该触发 breakeven 止损')

    def test_zero_is_not_treated_as_unset(self):
        """🔴 0 = 「我要求 ROAS=0（不设限）」，**不是**「没填」。

        用 `or` 读配置会把 0 当成没填、悄悄退回默认值 —— 那是另一种错。
        """
        self.assertIsNone(eng._target_roas({'target_roas': 0}))
        self.assertIsNone(eng._breakeven_roas({'breakeven_roas': 0}))
        self.assertIsNone(eng._target_cpa({'target_cpa': 0}))
        self.assertEqual(eng._target_roas({'target_roas': 2.5}), 2.5)

    # ---------- 加预算资格线 ----------
    def test_blocks_nomination_below_target_roas(self):
        """🔴 这就是那个真漏洞：净额为正但 ROAS 1.56 < 目标 2.0 ⇒ 不予提名。"""
        r = self._row()
        out, st = eng.enforce_risk_guardrails(self._sugg(), [r], {'target_roas': 2.0})
        self.assertEqual(st.get('blocked_by_target_roas'), 1)
        self.assertEqual(out[0]['nomination']['blocked_by'], 'target_roas')
        self.assertIn('不给它加预算', out[0]['reason'])

    def test_nomination_allowed_when_target_met(self):
        """ROAS 达标（3.5 ≥ 2.0）⇒ 提名照常发生（**要确认没被新资格线误拦**）。"""
        r = self._row(spend=300.0, rev=1050.0, orders=30)
        out, st = eng.enforce_risk_guardrails(self._sugg(), [r], {'target_roas': 2.0})
        self.assertNotEqual(st.get('blocked_by_target_roas'), 1,
                            '达标却被拦：%s' % out[0].get('reason'))
        self.assertIn('nomination', out[0], '提名应照常发生')

    # ---------- 止损线：breakeven 与 target 必须不同档 ----------
    def test_breakeven_catches_positive_net_but_below_breakeven(self):
        """🔴 核心：净额 **为正** 但 ROAS 1.56 < 盈亏线 2.0 ⇒ 强制停。
        「只看不亏」根本抓不到这类广告。"""
        r = self._row()
        out, st = eng.enforce_risk_guardrails(self._sugg('increase_budget'), [r],
                                              {'breakeven_roas': 2.0})
        self.assertEqual(out[0]['action'], 'pause', out[0].get('reason'))
        self.assertEqual(out[0]['guardrail'], 'forced_pause')
        self.assertIn('卖得越多亏得越多', out[0]['reason'])

    def test_target_roas_is_NOT_used_as_stoploss(self):
        """🔴 两档线必须分开：拿 target 当止损线，成批误杀「在赚但没到目标」的好广告。

        这条 ROAS 2.5：高于 breakeven(2.0) 所以不亏，但低于 target(3.0)。
        正确处置 = **保持不动**（不加预算也不关停），不是 pause。
        """
        r = self._row(spend=300.0, rev=750.0, orders=10)
        out, st = eng.enforce_risk_guardrails(self._sugg(), [r],
                                              {'breakeven_roas': 2.0, 'target_roas': 3.0})
        self.assertNotEqual(out[0].get('guardrail'), 'forced_pause',
                            '不该被止损：%s' % out[0].get('reason'))
        self.assertEqual(out[0]['action'], 'observe')

    def test_no_breakeven_keeps_net_line_only(self):
        """没填盈亏线 ⇒ 止损线退回 net<=0（旧行为，不悄悄变严）。"""
        r = self._row()
        out, st = eng.enforce_risk_guardrails(self._sugg('increase_budget'), [r],
                                              {'breakeven_roas': None})
        self.assertNotEqual(out[0].get('guardrail'), 'forced_pause')

    # ---------- 口径不适用时不得判 ----------
    def test_cart_opt_ad_is_exempt_from_roas_lines(self):
        """🔴 加购口径的广告，购买 ROAS 天然低 —— 用它卡线就是把靶子搞错。"""
        r = self._row(opt=self.CART)
        out, st = eng.enforce_risk_guardrails(self._sugg('increase_budget'), [r],
                                              {'breakeven_roas': 2.0, 'target_roas': 2.0})
        self.assertNotEqual(out[0].get('guardrail'), 'forced_pause',
                            '加购口径不该被购买 ROAS 止损：%s' % out[0].get('reason'))
        self.assertNotEqual(st.get('blocked_by_target_roas'), 1)

    # ---------- 缺数据不得当 0 ----------
    def test_missing_roas_is_not_treated_as_zero(self):
        """🔴 拿不到 ROAS ⇒ 不参与判定。**没数据不是「最坏」，绝不能当 0。**

        这里造的是**真正的缺列**（revenue 整个键不存在）。
        之前我第一版把 revenue 设成 0 —— 那组数据自相矛盾（0 收入却净额为正 +302），
        夹具不成立，测出来的是「夹具的问题」不是「代码的问题」。
        """
        r = self._row()
        r['net_view'].pop('roas', None)
        r['net_view'].pop('revenue', None)      # 缺列，不是 0
        out, st = eng.enforce_risk_guardrails(self._sugg('increase_budget'), [r],
                                              {'breakeven_roas': 2.0})
        self.assertNotEqual(out[0].get('guardrail'), 'forced_pause',
                            '数据缺失不等于 ROAS=0：%s' % out[0].get('reason'))

    def test_zero_roas_falls_through_to_recompute(self):
        """取数层在拿不到回收时可能落 `roas=0` ⇒ 必须回退到 revenue/spend 重算，
        否则一条**有真实回收**的广告会被当成 ROAS 最低而强制停。"""
        r = self._row(spend=300.0, rev=1050.0, orders=30)   # 真实 ROAS = 3.5
        r['net_view']['roas'] = 0.0          # 取数层落的 0，**不是**真的零
        self.assertAlmostEqual(eng._roas_of(r['net_view']), 3.5, places=2)

    # ---------- prompt 必须写进去 ----------
    def test_prompt_carries_target_values(self):
        snap = {'id': 'a1', 'name': 'X', 'status': 'ACTIVE', 'spend': 100.0,
                'clicks': 100, 'purchase': 0, 'daily_spend': []}
        p = eng.build_prompt([snap], {'breakeven_roas': 2.0, 'target_roas': 3.0},
                             window=('2026-04-01', '2026-04-05'))
        self.assertIn('我的目标值', p)
        self.assertIn('盈亏平衡 ROAS', p)
        self.assertIn('目标 ROAS', p)
        # 最要紧的一句：净额为正 ≠ 该加预算
        self.assertIn('净额为正', p)
        self.assertIn('加预算', p)

    def test_prompt_has_no_target_block_when_unset(self):
        """没填就不该出现这一段 —— 让 AI 以为有硬要求是更糟的错。"""
        snap = {'id': 'a1', 'name': 'X', 'status': 'ACTIVE', 'spend': 100.0,
                'clicks': 100, 'purchase': 0, 'daily_spend': []}
        p = eng.build_prompt([snap], {'target_roas': None, 'breakeven_roas': None},
                             window=('2026-04-01', '2026-04-05'))
        self.assertNotIn('我的目标值', p)

    def test_cpa_helpers_read_correctly(self):
        r = self._row(spend=540.0, rev=842.0, orders=24)
        self.assertAlmostEqual(eng._cpa_of(r['net_view'], r), 22.5, places=1)
        self.assertIsNone(eng._cpa_of(r['net_view'], {'purchase': 0, 'spend': 540.0}))
        self.assertIsNone(eng._target_cpa({}))

class TestTargetCpa(unittest.TestCase):
    """业务目标：「我多少钱出一单」（2026-10-03 接线；同日晚用户拍板改**仅做参考**）。

    🔴 2026-10-03 晚用户拍板：目标 CPA **仅做参考，不是绝对判定标准** ——
    超线**不再拦截**加预算提名（原「资格线②」作废），护栏只在结论里标注提醒
    （cpa_reference / cpa_reference_flagged），拦不拦由人工决定。
    零单 = CPA 无限大（≠ 0）的口径保持不变。
    """

    BUY = 'actions:offsite_conversion.fb_pixel_purchase'
    CART = 'actions:offsite_conversion.fb_pixel_add_to_cart'

    def _row(self, spend=300.0, rev=1050.0, orders=30, opt=None, **over):
        """默认造一条**达标**的：ROAS 3.5、CPA $10/单、净额为正、顶格、频次不饱和。"""
        r = {'id': 'a1', 'name': 'X', 'status': 'ACTIVE',
             'optimization_event': opt or self.BUY,
             'spend': spend, 'purchase_value': rev, 'purchase': orders,
             'clicks': 800, 'cost_per_purchase': spend / max(orders, 1)}
        r.update(over)
        r['net_view'] = {'net': round(rev - spend, 2), 'spend': spend, 'revenue': rev,
                         'roas': round(rev / spend, 2), 'fill_rate': 1.0,
                         'freq_first': 2.2, 'freq_last': 2.4, 'days': 5}
        return r

    def _sugg(self, act='observe'):
        return [{'campaign_id': 'a1', 'action': act, 'budget_change_pct': 0,
                 'reason': '模型原判'}]

    def test_unset_target_cpa_means_no_gate(self):
        """没填 ⇒ 参考线不生效（CPA 差几十倍，代码不能替用户猜）。"""
        r = self._row()
        out, st = eng.enforce_risk_guardrails(self._sugg(), [r], {'target_cpa': None})
        self.assertIsNone(st.get('cpa_reference_flagged'),
                          '未填时不该提醒：%s' % out[0].get('reason', ''))

    def test_cpa_over_target_flags_reference(self):
        """单均成本 $54 > 目标 $20 ⇒ **只标注提醒，不拦提名**（2026-10-03 晚拍板：仅做参考）。"""
        r = self._row(spend=540.0, rev=1620.0, orders=10)    # ROAS 3.0、CPA 54
        out, st = eng.enforce_risk_guardrails(self._sugg(), [r], {'target_cpa': 20.0})
        self.assertEqual(st.get('cpa_reference_flagged'), 1)
        self.assertIn('【目标参考】', out[0]['reason'])
        self.assertIn('仅做参考', out[0]['reason'])
        self.assertIn('nomination', out[0], '参考线不许拦提名')
        self.assertNotIn('blocked_by', out[0]['nomination'])

    def test_cpa_within_target_allows_nomination(self):
        """CPA $10 ≤ 目标 $20 ⇒ 提名照常发生（**确认没被新线误拦**）。"""
        r = self._row()
        out, st = eng.enforce_risk_guardrails(self._sugg(), [r], {'target_cpa': 20.0})
        self.assertIsNone(st.get('cpa_reference_flagged'),
                          '达标却被告警：%s' % out[0].get('reason'))
        self.assertIn('nomination', out[0])

    def test_cpa_reference_does_not_block_when_roas_met(self):
        """🔴 2026-10-03 晚拍板：ROAS 达标（3.0 ≥ 2.0）但 CPA 超标（54 > 20）⇒ 提名照常、只提醒。

        这就是「单少而每单很大」的情形 —— CPA 参考线把它点出来，
        拦不拦由人决定，代码不再代拦。
        """
        r = self._row(spend=540.0, rev=1620.0, orders=10)
        out, st = eng.enforce_risk_guardrails(self._sugg(), [r],
                                              {'target_roas': 2.0, 'target_cpa': 20.0})
        self.assertIsNone(st.get('blocked_by_target_roas'), 'ROAS 是达标的，不该由它拦')
        self.assertEqual(st.get('cpa_reference_flagged'), 1, 'CPA 超标应有参考提醒')
        self.assertIn('nomination', out[0], '参考线不许拦提名')

    def test_zero_orders_is_not_zero_cpa(self):
        """🔴 花了钱一单没出 ⇒ CPA 是「无限大」，**不是 0**（0 会被读成零成本、超划算）。

        夹具说明：purchase=0 但 purchase_value>0 不是我编的 ——
        归因窗口不一致时（订单落在窗口外、金额落在窗口内）真实导出就是这样。
        """
        r = self._row(spend=540.0, rev=842.0, orders=0, cost_per_purchase=0)
        self.assertIsNone(eng._cpa_of(r['net_view'], r), '零单时 CPA 必须是 None（≠0）')
        out, st = eng.enforce_risk_guardrails(self._sugg(), [r], {'target_cpa': 20.0})
        self.assertEqual(st.get('cpa_reference_flagged'), 1)
        self.assertIn('一单没出', out[0]['reason'])

    def test_cart_opt_ad_is_exempt_from_cpa_line(self):
        """加购口径的广告没有「购买单」可言 —— 拿购买 CPA 卡它就是把靶子搞错。"""
        r = self._row(opt=self.CART, spend=540.0, rev=1620.0, orders=10)
        out, st = eng.enforce_risk_guardrails(self._sugg(), [r], {'target_cpa': 20.0})
        self.assertIsNone(st.get('cpa_reference_flagged'),
                          '加购口径不该被购买 CPA 提醒：%s' % out[0].get('reason'))

    def test_prompt_carries_cpa_target(self):
        """CPA 目标要出现在 prompt 里，且写清楚「没单 ≠ 零成本」。"""
        snap = {'id': 'a1', 'name': 'X', 'status': 'ACTIVE', 'spend': 100.0,
                'clicks': 100, 'purchase': 0, 'daily_spend': []}
        p = eng.build_prompt([snap], {'target_cpa': 80.0},
                             window=('2026-04-01', '2026-04-05'))
        self.assertIn('目标 CPA', p)
        self.assertIn('一单没出', p)      # 「没单 ≠ CPA=0」这句必须在
        self.assertIn('80.00', p)
        self.assertIn('仅做参考', p)      # 2026-10-03 晚：参考线口径必须在


if __name__ == '__main__':
    unittest.main(verbosity=2)
