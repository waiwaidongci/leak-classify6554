"""封存研究迁出：数据集 + 窗口 + 成功运行（含训练文件）+ 策略 + 人工复核。

不触发任何训练；只搬运已封存结果。失败/进行中的运行不属于可迁出研究。
"""
import json
from datetime import datetime, timezone

from sqlalchemy import select
from fastapi import HTTPException

from src.models import (
    WindowDataset, SensorWindow, TrainingRun, ThresholdPolicy,
    InspectionReview, User,
)
from src.config import settings
from src.services.audit import record
from src.services.fingerprint import fingerprint
from .bundle import (
    BUNDLE_VERSION, MANIFEST_NAME, canonical_json, write_bundle,
    window_digest, algorithm_tag,
)


def _run_artifact_path(run_id: int):
    from pathlib import Path
    return Path(settings.ARTIFACT_DIR) / f'training-{run_id}.json'


def export_dataset(db, dataset_id: int, user) -> tuple[bytes, str, dict]:
    dataset = db.get(WindowDataset, dataset_id)
    if not dataset:
        raise HTTPException(404, '数据集不存在')
    window_rows = [
        {'id': w.id, 'reference': w.reference, 'features': w.features, 'label': w.label}
        for w in db.scalars(
            select(SensorWindow).where(SensorWindow.dataset_id == dataset_id)
            .order_by(SensorWindow.reference))
    ]
    if not window_rows:
        raise HTTPException(422, '数据集没有窗口，无法封存')

    runs = list(db.scalars(
        select(TrainingRun).where(TrainingRun.dataset_id == dataset_id)
        .order_by(TrainingRun.id)))
    succeeded = [r for r in runs if r.status == 'succeeded' and r.result]
    if not succeeded:
        raise HTTPException(422, '该数据集没有已完成的研究运行，无可迁出封存结果')

    # 原指纹在迁出前先本地复算一次，指纹失配说明库内结果已无法追溯，拒绝打包。
    for run in succeeded:
        expected = fingerprint(window_rows, run.config)
        if run.result.get('fingerprint') != expected:
            raise HTTPException(
                422, '运行 %s 的输入指纹与当前窗口复算结果不符，拒绝迁出' % run.id)

    run_ids = [r.id for r in succeeded]
    policies = list(db.scalars(
        select(ThresholdPolicy).where(ThresholdPolicy.run_id.in_(run_ids))
        .order_by(ThresholdPolicy.id)))
    policy_ids = [p.id for p in policies]
    reviews = list(db.scalars(
        select(InspectionReview).where(InspectionReview.policy_id.in_(policy_ids))
        .order_by(InspectionReview.id))) if policy_ids else []
    reviewer_cache = {u.id: u.email for u in db.scalars(select(User))}

    members: dict[str, bytes] = {}
    members['dataset.json'] = canonical_json({
        'name': dataset.name,
        'filename': dataset.filename,
        'description': dataset.description,
    })
    members['windows.json'] = canonical_json([
        {'source_window_id': w['id'], 'reference': w['reference'],
         'features': w['features'], 'label': w['label']}
        for w in window_rows
    ])

    run_payloads, artifact_payloads = [], []
    for run in succeeded:
        path = _run_artifact_path(run.id)
        if path.exists():
            artifact = json.loads(path.read_text(encoding='utf-8'))
            artifact_source = 'artifact_volume'
            if artifact != run.result:
                raise HTTPException(
                    422, '运行 %s 的训练文件与数据库结果不一致，拒绝迁出' % run.id)
        else:
            # 共享卷缺失时以库内封存 JSON 重建，内容与落库结果严格一致。
            artifact = run.result
            artifact_source = 'database_result'
        run_payloads.append({
            'source_run_id': run.id,
            'name': run.name,
            'status': run.status,
            'config': run.config,
            'result': run.result,
            'creator_email': reviewer_cache.get(run.creator_id, ''),
            'created_at': run.created_at.isoformat(),
        })
        artifact_payloads.append((run.id, artifact, artifact_source))
        members[f'runs/run-{run.id}.json'] = canonical_json(run_payloads[-1])
        members[f'artifacts/training-{run.id}.json'] = canonical_json(artifact)

    members['policies.json'] = canonical_json([
        {'source_policy_id': p.id, 'source_run_id': p.run_id, 'name': p.name,
         'threshold': p.threshold, 'status': p.status, 'note': p.note,
         'creator_email': reviewer_cache.get(p.creator_id, ''),
         'created_at': p.created_at.isoformat()}
        for p in policies
    ])
    members['reviews.json'] = canonical_json([
        {'source_policy_id': v.policy_id, 'source_window_id': v.window_id,
         'verdict': v.verdict, 'note': v.note,
         'reviewer_email': reviewer_cache.get(v.reviewer_id, ''),
         'created_at': v.created_at.isoformat()}
        for v in reviews
    ])

    exported_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        'bundle_version': BUNDLE_VERSION,
        'algorithm': algorithm_tag(),
        'origin_station': settings.STATION_NAME,
        'exported_at': exported_at,
        'exported_by': user.email,
        'dataset_name': dataset.name,
        'window_count': len(window_rows),
        'window_digest': window_digest(window_rows),
        'run_count': len(run_payloads),
        'policy_count': len(policies),
        'review_count': len(reviews),
        'fingerprints': [
            {'source_run_id': r['source_run_id'], 'fingerprint': r['result']['fingerprint']}
            for r in run_payloads
        ],
        'artifact_sources': [
            {'source_run_id': sid, 'source': source}
            for sid, _, source in artifact_payloads
        ],
    }
    members[MANIFEST_NAME] = canonical_json(manifest)
    blob, final_manifest = write_bundle(members)
    record(db, user.email, 'migration.exported', {
        'dataset_id': dataset_id, 'package_digest': final_manifest['package_digest'],
        'runs': len(run_payloads), 'policies': len(policies), 'reviews': len(reviews),
    })
    safe_name = dataset.name.replace('/', '_').replace('\\', '_')
    filename = 'research-bundle-%s-%s.zip' % (
        safe_name, datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    return blob, filename, final_manifest
