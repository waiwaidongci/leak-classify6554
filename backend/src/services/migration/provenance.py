"""封存研究迁入后的可追溯复核：原指纹、逐窗判别、训练文件与引用完整性。

不依赖原始 zip：全部依据目标库实际数据与导入登记中保留的源映射重建。
"""
from sqlalchemy import select
from fastapi import HTTPException

from src.models import (
    WindowDataset, SensorWindow, TrainingRun, ThresholdPolicy,
    InspectionReview,
)
from src.services.fingerprint import fingerprint
from src.services.datasets import get_windows
from .bundle import window_digest


def verify_imported_run(db, run: TrainingRun, origin: dict) -> dict:
    """对一个迁入运行执行完整复核，返回核验明细；任何不符抛 422。"""
    problems = []
    dataset = db.get(WindowDataset, run.dataset_id)
    target_windows = get_windows(db, run.dataset_id)
    target_by_id = {w['id']: w for w in target_windows}

    # 1) 原指纹可追溯：用源窗口 id 映射回目标窗口后，按封存输入口径重新计算。
    window_map = origin.get('window_id_map') or {}
    source_rows = []
    for source_id_str, target_id in window_map.items():
        w = target_by_id.get(target_id)
        if not w:
            problems.append('源窗口 %s 的目标映射缺失' % source_id_str)
            continue
        source_rows.append({'id': int(source_id_str), 'reference': w['reference'],
                            'features': w['features'], 'label': w['label']})
    if len(source_rows) != origin.get('window_count'):
        problems.append('窗口映射数量与封存计数不一致')
    recomputed = fingerprint(source_rows, run.config) if source_rows else None
    stored = run.result.get('fingerprint')
    source_fp = origin.get('source_fingerprints', {}).get(str(run.id))
    if recomputed != stored:
        problems.append('目标窗口重算指纹与运行封存指纹不符')
    if source_fp and source_fp != stored:
        problems.append('迁入登记保留的原指纹与运行封存指纹不符')

    # 2) 逐窗判别可追溯：留出集 id 全部指向本数据集且与逐窗判别一一对应。
    split_ids = run.result.get('split_ids', {})
    dataset_ids = set(target_by_id)
    all_split, seen = [], set()
    for group in ('train', 'validation', 'test'):
        ids = split_ids.get(group, [])
        if not ids or set(ids) - dataset_ids:
            problems.append('%s 分割引用了数据集之外的窗口' % group)
        if seen.intersection(ids):
            problems.append('%s 分割与其他组相交' % group)
        seen.update(ids)
        all_split.extend(ids)
    if len(all_split) != len(dataset_ids) or set(all_split) != dataset_ids:
        problems.append('分割未恰好覆盖数据集全部窗口')
    predictions = run.result.get('test_predictions', [])
    test_set = set(split_ids.get('test', []))
    if {p['window_id'] for p in predictions} != test_set:
        problems.append('逐窗判别与留出窗口集合不一致')
    ref_index = {w['id']: w['reference'] for w in target_windows}
    for p in predictions:
        if p.get('reference') != ref_index.get(p['window_id']):
            problems.append('逐窗判别 reference 与目标窗口不符')
            break

    # 3) 训练文件与库内结果逐键一致。
    import json
    from pathlib import Path
    from src.config import settings
    path = Path(settings.ARTIFACT_DIR) / f'training-{run.id}.json'
    if not path.exists():
        problems.append('训练文件 training-%s.json 不可见' % run.id)
    elif json.loads(path.read_text(encoding='utf-8')) != run.result:
        problems.append('训练文件与数据库结果不一致')

    # 4) 策略阈值仍逐字复制验证阈值，复核只落在该策略的留出候选上。
    locked = run.result['recommendation']['threshold']
    policies = db.scalars(select(ThresholdPolicy).where(
        ThresholdPolicy.run_id == run.id)).all()
    candidate_ids = {p['window_id'] for p in predictions if p['predicted'] == 1}
    for policy in policies:
        if float(policy.threshold) != float(locked):
            problems.append('策略 %s 阈值偏离运行验证阈值' % policy.id)
        for review in db.scalars(select(InspectionReview).where(
                InspectionReview.policy_id == policy.id)).all():
            if review.window_id not in candidate_ids:
                problems.append('策略 %s 存在越界复核窗口 %s' % (policy.id, review.window_id))

    return {
        'run_id': run.id,
        'run_name': run.name,
        'fingerprint': stored,
        'fingerprint_recomputed': recomputed,
        'fingerprint_matches_origin': source_fp == stored if source_fp else None,
        'window_digest': window_digest(target_windows),
        'prediction_count': len(predictions),
        'split_counts': run.result.get('split_counts'),
        'policy_count': len(policies),
        'ok': not problems,
        'problems': problems,
    }


def assert_imported_research(db, run: TrainingRun, origin: dict):
    report = verify_imported_run(db, run, origin)
    if not report['ok']:
        raise HTTPException(422, '迁入研究核验失败: ' + '；'.join(report['problems']))
    return report
