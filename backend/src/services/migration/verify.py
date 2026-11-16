"""封存包业务结构与交叉引用校验；所有规则失败都必须整包拒收。"""
from src.constants.enums import RunStatus, PolicyStatus, Verdict
from src.constants.features import FEATURES
from .bundle import BundleError

REVIEW_VERDICTS = {Verdict.PENDING, Verdict.CONFIRMED, Verdict.FALSE_ALARM}
POLICY_STATUSES = {PolicyStatus.DRAFT, PolicyStatus.APPROVED, PolicyStatus.REJECTED}


def _require(condition, message):
    if not condition:
        raise BundleError(message)


def _validate_windows(windows):
    _require(isinstance(windows, list) and windows, 'windows.json 必须是非空数组')
    refs, source_ids = set(), set()
    for w in windows:
        _require(isinstance(w, dict), '窗口记录格式非法')
        ref = w.get('reference')
        _require(isinstance(ref, str) and ref and ref not in refs, '窗口 reference 缺失或重复')
        refs.add(ref)
        sid = w.get('source_window_id')
        _require(isinstance(sid, int) and sid > 0 and sid not in source_ids,
                 '窗口 source_window_id 缺失或重复: %s' % ref)
        source_ids.add(sid)
        features = w.get('features')
        _require(isinstance(features, dict) and set(features) == set(FEATURES),
                 '窗口必须恰有六个允许特征: %s' % ref)
        _require(all(isinstance(v, (int, float)) for v in features.values()),
                 '窗口特征必须是数值: %s' % ref)
        _require(w.get('label') in (0, 1), '窗口 label 必须是 0/1: %s' % ref)
    return refs, source_ids


def _validate_run(run, window_refs, source_window_ids):
    _require(isinstance(run, dict), '运行记录格式非法')
    sid = run.get('source_run_id')
    _require(isinstance(sid, int) and sid > 0, '运行缺少合法 source_run_id')
    _require(run.get('status') == RunStatus.SUCCEEDED, '只允许封存成功运行，禁止重训/补算')
    config = run.get('config')
    _require(isinstance(config, dict)
             and isinstance(config.get('feature_sets'), list) and config['feature_sets']
             and isinstance(config.get('c_values'), list) and config['c_values']
             and isinstance(config.get('false_negative_cost'), (int, float))
             and isinstance(config.get('false_positive_cost'), (int, float)),
             '运行 %s 的训练配置不完整' % sid)
    for fs in config['feature_sets']:
        _require(isinstance(fs, list) and fs and len(set(fs)) == len(fs)
                 and set(fs) <= set(FEATURES), '运行 %s 含非法特征候选' % sid)
    result = run.get('result')
    _require(isinstance(result, dict) and isinstance(result.get('fingerprint'), str)
             and len(result['fingerprint']) == 64, '运行 %s 缺少原指纹' % sid)

    split_ids = result.get('split_ids')
    _require(isinstance(split_ids, dict) and set(split_ids) == {'train', 'validation', 'test'},
             '运行 %s 分割索引结构非法' % sid)
    seen = set()
    total = 0
    for group, ids in split_ids.items():
        _require(isinstance(ids, list) and ids and all(isinstance(i, int) for i in ids),
                 '运行 %s 的 %s 分割索引非法' % (sid, group))
        _require(len(ids) == len(set(ids)), '运行 %s 的 %s 分割存在重复窗口' % (sid, group))
        overlap = seen.intersection(ids)
        _require(not overlap, '运行 %s 分割组不相交性被破坏' % sid)
        seen.update(ids)
        total += len(ids)
    _require(seen <= source_window_ids,
             '运行 %s 的 split_ids 引用了不存在的源窗口' % sid)
    _require(total == len(window_refs), '运行 %s 分割窗口数与数据集不一致' % sid)

    predictions = result.get('test_predictions')
    test_ids = set(split_ids['test'])
    _require(isinstance(predictions, list) and len(predictions) == len(test_ids),
             '运行 %s 逐窗判别数与留出组不一致' % sid)
    pred_ids = set()
    for p in predictions:
        _require(isinstance(p, dict), '运行 %s 逐窗判别格式非法' % sid)
        wid = p.get('window_id')
        _require(isinstance(wid, int) and wid in test_ids,
                 '运行 %s 逐窗判别引用了非留出窗口' % sid)
        _require(wid not in pred_ids, '运行 %s 逐窗判别窗口重复' % sid)
        pred_ids.add(wid)
        _require(isinstance(p.get('reference'), str) and p['reference'] in window_refs,
                 '运行 %s 逐窗判别 reference 与窗口表不符' % sid)
        _require(isinstance(p.get('probability'), (int, float)),
                 '运行 %s 逐窗判别缺少概率' % sid)
        _require(p.get('predicted') in (0, 1), '运行 %s 逐窗判别标记非法' % sid)
    return sid


def _validate_policy(policy, run_ids):
    _require(isinstance(policy, dict), '策略记录格式非法')
    pid = policy.get('source_policy_id')
    rid = policy.get('source_run_id')
    _require(isinstance(pid, int) and pid > 0, '策略缺少合法 source_policy_id')
    _require(rid in run_ids, '策略 %s 引用了不在包内的运行' % pid)
    _require(policy.get('status') in POLICY_STATUSES, '策略 %s 状态非法' % pid)
    _require(isinstance(policy.get('threshold'), (int, float)),
             '策略 %s 缺少固定阈值' % pid)
    _require(isinstance(policy.get('name'), str) and policy['name'],
             '策略 %s 缺少名称' % pid)


def _validate_review(review, policy_ids, source_window_ids):
    _require(isinstance(review, dict), '复核记录格式非法')
    _require(review.get('source_policy_id') in policy_ids, '复核引用了不在包内的策略')
    wid = review.get('source_window_id')
    _require(isinstance(wid, int) and wid in source_window_ids,
             '复核引用了不存在的源窗口')
    _require(review.get('verdict') in REVIEW_VERDICTS, '复核结论非法')
    _require(isinstance(review.get('note'), str), '复核说明必须是字符串')
    _require(isinstance(review.get('reviewer_email'), str) and review['reviewer_email'],
             '复核缺少原复核人标识')


def validate_bundle_content(manifest, dataset, windows, runs, artifacts, policies, reviews):
    _require(isinstance(dataset, dict) and isinstance(dataset.get('name'), str)
             and dataset['name'], 'dataset.json 缺少数据集名称')
    window_refs, source_window_ids = _validate_windows(windows)
    _require(manifest.get('window_count') == len(windows), 'manifest 窗口计数不符')
    _require(manifest.get('window_digest'), 'manifest 缺少 window_digest')

    run_ids = set()
    _require(isinstance(runs, list) and runs, '封存包至少包含一个成功运行，否则不构成可迁入研究')
    _require(isinstance(manifest.get('fingerprints'), list)
             and len(manifest['fingerprints']) == len(runs),
             'manifest 指纹清单缺失或与运行数不符')
    for run in runs:
        sid = _validate_run(run, window_refs, source_window_ids)
        _require(sid not in run_ids, '运行 source_run_id 重复')
        run_ids.add(sid)
        artifact = artifacts.get(sid)
        _require(isinstance(artifact, dict), '运行 %s 缺少训练产物文件' % sid)
        # 训练文件与落库结果必须逐键一致（同一份 JSON 两个落点）。
        if artifact != run['result']:
            raise BundleError('运行 %s 的训练文件与数据库结果不一致' % sid)
        _require(artifact.get('fingerprint') == run['result']['fingerprint'],
                 '运行 %s 训练文件指纹不一致' % sid)

    _require(manifest.get('run_count') == len(runs), 'manifest 运行计数不符')

    policy_keys = set()
    for policy in policies:
        _validate_policy(policy, run_ids)
        key = (policy['source_policy_id'], policy['source_run_id'])
        _require(key not in policy_keys, '策略 source_policy_id 重复')
        policy_keys.add(key)
    _require(manifest.get('policy_count') == len(policies), 'manifest 策略计数不符')

    policy_ids = {p['source_policy_id'] for p in policies}
    review_keys = set()
    for review in reviews:
        _validate_review(review, policy_ids, source_window_ids)
        key = (review['source_policy_id'], review['source_window_id'])
        _require(key not in review_keys, '同一策略窗口存在重复复核')
        review_keys.add(key)
    _require(manifest.get('review_count') == len(reviews), 'manifest 复核计数不符')

    # 策略阈值必须逐字复制自对应运行的验证集选定阈值。
    run_by_id = {r['source_run_id']: r for r in runs}
    for policy in policies:
        locked = run_by_id[policy['source_run_id']]['result']['recommendation']['threshold']
        if float(policy['threshold']) != float(locked):
            raise BundleError('策略 %s 阈值与运行验证阈值不符' % policy['source_policy_id'])
