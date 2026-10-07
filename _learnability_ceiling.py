# -*- coding: utf-8 -*-
"""_learnability_ceiling.py —— 量化「反事实税」。

问题
====
这套工具想学的「正确动作」里，「加预算」这一档的判据是**反事实**的：
出题口径 = `差额 = 加预算净 − 不动净 > 0`。真实业务里你只观测到一条轨迹，
**永远不知道「如果不加会怎样」**。那么：

    用【可观测特征】去逼近【反事实标签】，最多能到多少？

三层剥开
========
  A. 作弊上限：直接用真值表「正确动作」当标签训练。
     —— 任何只用可见列的方法的理论天花板，但真实场景**拿不到这个标签**。
  B. 诚实上限：用可观测标签 `derive_label`（可见净 + 顶格率）训练，
     再对真值表「正确动作」判分 —— 这才是能部署的。
  C. 反事实税 = A − B。**这部分差距不是模型不够强，是标签不可观测造成的。**

再按动作档拆开：
  · 暂停档   —— 标签是可见净（可观测），应当接近可学；
  · 加预算档 —— 标签是差额（反事实），才是不可学的那部分。

为什么内置一个 CART 而不是直接用仓库的随机森林
==============================================
当前环境没有 scikit-learn（`_solvability_v5.py` 需要它）。为了**无依赖可复现**，
这里自带一个纯标准库 CART + 分层 K 折交叉验证。
关键点：A 和 B **必须用同一个模型**，这样两者之差才归因于标签、而不是模型。
（有 sklearn 时可 `--rf` 用随机森林复算，与 `_solvability_v5.py` 对齐。）

输出：stdout。不写文件、不落盘、不读任何含凭据的配置。
"""
import argparse
import csv
import io
import os
import random
import sys
from collections import Counter

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)

import flywheel
import table_judger as tj

try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import StratifiedKFold
    HAS_SK = True
except ImportError:
    HAS_SK = False

P_DATA = os.path.join(D, '模拟广告数据-100广告10天-v5.csv')
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_HID = os.path.join(D, '真值表-逐日-v5.csv')

ORACLE_MAP = {'暂停': 'pause', '加预算': 'increase_budget', '观察': 'observe'}
CN = {'pause': '暂停', 'increase_budget': '加预算', 'observe': '观察'}


# ==================== 纯标准库 CART ====================

def _gini(labels):
    n = len(labels)
    if n == 0:
        return 0.0
    c = Counter(labels)
    return 1.0 - sum((v / n) ** 2 for v in c.values())


def _candidate_thresholds(vals, n_quantiles):
    vals = sorted(set(vals))
    if len(vals) < 2:
        return []
    if len(vals) <= n_quantiles + 1:
        return [(vals[i] + vals[i + 1]) / 2.0 for i in range(len(vals) - 1)]
    out = []
    for q in range(1, n_quantiles):
        idx = int(round(q * len(vals) / n_quantiles))
        if 0 < idx < len(vals):
            out.append((vals[idx - 1] + vals[idx]) / 2.0)
    return sorted(set(out))


def build_tree(X, y, depth=0, max_depth=5, min_leaf=3, n_quantiles=16):
    """返回一棵决策树（dict）。叶子：{'label': ...}；内部：{'j','th','left','right'}。"""
    leaf = {'label': Counter(y).most_common(1)[0][0] if y else 'observe'}
    if (depth >= max_depth or len(y) < 2 * min_leaf
            or len(set(y)) == 1 or not X):
        return leaf
    parent = _gini(y)
    best = None
    nf = len(X[0])
    for j in range(nf):
        col = [row[j] for row in X]
        for th in _candidate_thresholds(col, n_quantiles):
            ly = [y[k] for k in range(len(y)) if col[k] <= th]
            if len(ly) < min_leaf or len(y) - len(ly) < min_leaf:
                continue
            ry = [y[k] for k in range(len(y)) if col[k] > th]
            w = (len(ly) * _gini(ly) + len(ry) * _gini(ry)) / len(y)
            gain = parent - w
            if best is None or gain > best[0]:
                best = (gain, j, th)
    if best is None or best[0] <= 1e-9:
        return leaf
    _, j, th = best
    lX = [X[k] for k in range(len(X)) if X[k][j] <= th]
    ly = [y[k] for k in range(len(X)) if X[k][j] <= th]
    rX = [X[k] for k in range(len(X)) if X[k][j] > th]
    ry = [y[k] for k in range(len(X)) if X[k][j] > th]
    leaf['j'] = j
    leaf['th'] = th
    leaf['left'] = build_tree(lX, ly, depth + 1, max_depth, min_leaf, n_quantiles)
    leaf['right'] = build_tree(rX, ry, depth + 1, max_depth, min_leaf, n_quantiles)
    return leaf


def predict_tree(node, x):
    while 'j' in node:
        node = node['left'] if x[node['j']] <= node['th'] else node['right']
    return node['label']


def stratified_folds(y, k, seed):
    by = {}
    for i, lab in enumerate(y):
        by.setdefault(lab, []).append(i)
    rng = random.Random(seed)
    folds = [[] for _ in range(k)]
    for lab in sorted(by):
        idxs = by[lab][:]
        rng.shuffle(idxs)
        for n, i in enumerate(idxs):
            folds[n % k].append(i)
    return [f for f in folds if f]


def cv_predict(X, y_train, k=5, max_depth=5, seed=7, min_leaf=3, n_quantiles=16,
               model='auto'):
    """分层 K 折**折外**预测，返回与 X 等长的预测列表。

    model='auto'：有 sklearn 用随机森林（与 _solvability_v5.py 对齐），否则 CART。
    A / B 两层必须用**同一个** model，差值才归因于标签。
    """
    use_rf = (model == 'rf') or (model == 'auto' and HAS_SK)
    if use_rf:
        if not HAS_SK:
            raise RuntimeError('要用随机森林请先 pip install scikit-learn')
        skf = StratifiedKFold(n_splits=k, shuffle=True, random_state=seed)
        preds = [None] * len(X)
        for tr, te in skf.split(X, y_train):
            m = RandomForestClassifier(n_estimators=400, random_state=1)
            m.fit([X[i] for i in tr], [y_train[i] for i in tr])
            for i, p in zip(te, m.predict([X[i] for i in te])):
                preds[i] = str(p)
        return preds
    folds = stratified_folds(y_train, k, seed)
    preds = [None] * len(X)
    for f in folds:
        te = set(f)
        tr = [i for i in range(len(X)) if i not in te]
        tree = build_tree([X[i] for i in tr], [y_train[i] for i in tr],
                          0, max_depth, min_leaf, n_quantiles)
        for i in f:
            preds[i] = predict_tree(tree, X[i])
    return preds


def accuracy(preds, y, mask=None):
    idx = range(len(y)) if mask is None else [i for i in range(len(y)) if mask[i]]
    n = len(idx)
    if n == 0:
        return 0.0, 0
    hit = sum(1 for i in idx if preds[i] == y[i])
    return hit / n * 100.0, n


def recall_for(preds, y, cls, mask=None):
    idx = [i for i in range(len(y)) if y[i] == cls and (mask is None or mask[i])]
    if not idx:
        return None, 0
    hit = sum(1 for i in idx if preds[i] == cls)
    return hit / len(idx) * 100.0, len(idx)


# ==================== 数据装配 ====================

def load_xy(limit_rf=None):
    """返回 (names, X, y_oracle, y_obs, feats)。标签均为英文三分类。"""
    recs = flywheel.build_records_from_truth(P_DATA, P_TRUTH, P_HID)
    truth = tj.load_truth_v5(P_TRUTH)
    names, X, y_oracle, y_obs, feats = [], [], [], [], []
    for r in recs:
        t = truth.get(r['ad_name'])
        if not t or t.get('正确动作') not in ORACLE_MAP:
            continue
        oracle = ORACLE_MAP[t['正确动作']]
        obs = tj.derive_label(r.get('outcome') or {})
        if obs is None:
            continue
        f = r['features']
        names.append(r['ad_name'])
        X.append([f.get(k, 0.0) for k in tj.FEATURE_KEYS])
        y_oracle.append(oracle)
        y_obs.append(obs)
        feats.append(f)
    return names, X, y_oracle, y_obs, feats


def rule_predict(feats):
    """仓库规则基线：可见净 + 顶格率（花费率≥0.95 近似顶格）+ 末段频次。"""
    out = []
    for f in feats:
        vis = f.get('收入', 0.0) - f.get('花费', 0.0)
        try:
            fr = 1.0 if float(f.get('花费率', 0)) >= 0.95 else 0.0
        except (TypeError, ValueError):
            fr = None
        out.append(tj.rule_action(net=vis, fill_rate=fr, freq_last=f.get('频次末')))
    return out


# ==================== 报告 ====================

def main(argv=None):
    ap = argparse.ArgumentParser(description='量化「反事实税」：可观测标签 vs 反事实标签')
    ap.add_argument('--folds', type=int, default=5)
    ap.add_argument('--depth', type=int, default=6)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--n-quantiles', type=int, default=16)
    ap.add_argument('--model', choices=['auto', 'rf', 'cart'], default='auto',
                    help='auto=有 sklearn 用随机森林，否则 CART')
    args = ap.parse_args(argv)

    names, X, y_oracle, y_obs, feats = load_xy()
    n = len(y_oracle)
    used = '随机森林' if (args.model == 'rf' or (args.model == 'auto' and HAS_SK)) else 'CART 深%d' % args.depth
    print('=' * 78)
    print('反事实税测量 · %d 条模拟广告 · %d 维可见特征 · %d 折折外 · 模型=%s'
          % (n, len(tj.FEATURE_KEYS), args.folds, used))
    print('=' * 78)

    # 0. 标签一致率
    agree = sum(1 for i in range(n) if y_obs[i] == y_oracle[i])
    print()
    print('【0】可观测标签(derive_label v2) vs 反事实标签(真值表正确动作)')
    print('    一致率：%d/%d = %.1f%%' % (agree, n, agree / n * 100))
    print('    oracle 分布：%s' % _dist(y_oracle))
    print('    obs    分布：%s' % _dist(y_obs))

    # 基线：恒定判多数类
    maj = Counter(y_oracle).most_common(1)[0][0]
    maj_acc = Counter(y_oracle).most_common(1)[0][1] / n * 100
    print()
    print('【基线】恒定判「%s」→ %.1f%%' % (CN[maj], maj_acc))
    rule_preds = rule_predict(feats)
    rule_acc, _ = accuracy(rule_preds, y_oracle)
    print('       仓库规则基线          → %.1f%%' % rule_acc)

    # A. 作弊上限
    preds_oracle = cv_predict(X, y_oracle, k=args.folds, max_depth=args.depth,
                              seed=args.seed, n_quantiles=args.n_quantiles, model=args.model)
    acc_a, _ = accuracy(preds_oracle, y_oracle)

    # B. 诚实上限：用可观测标签训练，对反事实标签判分
    preds_obs = cv_predict(X, y_obs, k=args.folds, max_depth=args.depth,
                           seed=args.seed, n_quantiles=args.n_quantiles, model=args.model)
    acc_b, _ = accuracy(preds_obs, y_oracle)

    print()
    print('【主结果】同一个模型（%s），只换训练标签：' % used)
    print('    A 作弊上限（用反事实标签训练）  →  %.1f%%' % acc_a)
    print('    B 诚实上限（用可观测标签训练）  →  %.1f%%' % acc_b)
    print('    C 反事实税 = A − B              →  %.1f 个百分点' % (acc_a - acc_b))

    # 分档：暂停（可观测）vs 加预算（反事实）
    print()
    print('【分档拆解】')
    print('    暂停档：oracle 暂停的 %d 条里，规则/模型各自命中多少'
          % sum(1 for v in y_oracle if v == 'pause'))
    for tag, preds in [('规则基线', rule_preds), ('A 模型', preds_oracle), ('B 模型', preds_obs)]:
        r, m = recall_for(preds, y_oracle, 'pause')
        print('      %-8s 暂停召回 %s' % (tag, _pct(r, m)))

    nonpause = [v != 'pause' for v in y_oracle]
    print('    加预算 vs 观察（在 %d 条非暂停广告里）：' % sum(nonpause))
    for tag, preds in [('规则基线', rule_preds), ('A 模型', preds_oracle), ('B 模型', preds_obs)]:
        r, m = recall_for(preds, y_oracle, 'increase_budget', mask=nonpause)
        acc_np, _ = accuracy(preds, y_oracle, mask=nonpause)
        print('      %-8s 加预算召回 %s · 非暂停子集准确率 %.1f%%'
              % (tag, _pct(r, m), acc_np))

    print()
    print('【读法】')
    print('    · 暂停档：标签 = 可见净（可观测），规则/A/B 都该接近满分 —— 这部分是"能学的"。')
    print('    · 加预算档：标签 = 差额（反事实），A 明显高于 B 的差距就是反事实税。')
    print('    · B 才是能部署的真实水平；A 只存在于模拟数据里，**不要拿 A 当业绩**。')
    print('=' * 78)
    return 0


def _dist(y):
    c = Counter(y)
    return ' / '.join('%s %d' % (CN.get(k, k), c[k]) for k in ['pause', 'increase_budget', 'observe'] if c.get(k))


def _pct(v, n):
    return '—' if v is None else '%.0f%% (%d)' % (v, n)


if __name__ == '__main__':
    sys.exit(main())
