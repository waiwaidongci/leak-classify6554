"""封存研究迁入：整包校验通过后，在单个数据库事务内重建全部引用。

安全性质：
- 篡改/缺件/交叉引用不符 -> 抛 BundleError，不写任何业务数据，整包拒收；
- 同名异值数据集 -> 加来源后缀隔离保存，目标原有研究保持原样；
- 所有窗口/运行/策略引用按目标库实际主键重建，源库数字 id 不沿用；
- 数据库事务与训练文件同生共死：先暂存文件，提交后再原子改名，
  启动时回收任何未完成登记与孤儿暂存文件，保证“均可用或均不可见”；
- package_digest 部分唯一索引 + 状态登记，并发导入同一包只落一份，可重试。
"""
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException

from src.config.database import SessionLocal
from src.config import settings
from src.constants.enums import RunStatus
from src.models import (
    WindowDataset, SensorWindow, TrainingRun, ThresholdPolicy,
    InspectionReview, User, MigrationImport,
)
from src.services.audit import record
from .bundle import read_bundle, json_member, window_digest, BundleError
from .verify import validate_bundle_content
from .remap import remap_run_result

_SAFE_NAME = re.compile(r'[^0-9A-Za-z一-鿿·._-]+')


class ImportRejected(HTTPException):
    def __init__(self, message):
        super().__init__(422, message)


class ImportDuplicate(HTTPException):
    def __init__(self, record_obj):
        super().__init__(409, '该封存包已导入，幂等返回既有迁入记录')
        self.record = record_obj


def _unique_dataset_name(db, base: str) -> str:
    candidate = base[:100]
    suffix_no = 2
    while db.scalar(select(WindowDataset).where(WindowDataset.name == candidate)):
        tag = '（迁入%s）' % suffix_no
        candidate = (base[:100 - len(tag)] + tag)
        suffix_no += 1
    return candidate


def _parse_package(raw: bytes):
    if len(raw) > 256 * 1024 * 1024:
        raise ImportRejected('封存包超过 256MB 上限')
    try:
        manifest, members = read_bundle(raw)
        dataset = json_member(members, 'dataset.json')
        windows = json_member(members, 'windows.json')
        try:
            runs = [json_member(members, 'runs/run-%s.json' % p.split('/')[-1][4:-5])
                    for p in sorted(x for x in members if x.startswith('runs/'))]
            artifacts = {
                int(p.split('/')[-1][9:-5]): json_member(members, p)
                for p in members if p.startswith('artifacts/')
            }
        except ValueError as exc:
            raise BundleError('封存包成员命名非法，无法解析运行编号') from exc
        policies = json_member(members, 'policies.json')
        reviews = json_member(members, 'reviews.json')
        validate_bundle_content(manifest, dataset, windows, runs, artifacts, policies, reviews)
    except BundleError as exc:
        raise ImportRejected(str(exc)) from exc
    if window_digest([{'reference': w['reference'], 'features': w['features'],
                       'label': w['label']} for w in windows]) != manifest['window_digest']:
        raise ImportRejected('窗口内容摘要不符，封存包可能被篡改')
    return manifest, dataset, windows, runs, artifacts, policies, reviews


def _load_package(db, package_digest: str):
    existing = db.scalar(select(MigrationImport).where(
        MigrationImport.package_digest == package_digest,
        MigrationImport.status == 'succeeded'))
    if existing:
        raise ImportDuplicate(existing)


def import_bundle(raw: bytes, user) -> dict:
    parsed = _parse_package(raw)
    manifest = parsed[0]
    package_digest = manifest['package_digest']

    # 阶段一：登记并抢占幂等锁。唯一索引兜底跨进程并发，只允许一个导入者。
    db = SessionLocal()
    try:
        _load_package(db, package_digest)
        registration = MigrationImport(
            package_digest=package_digest, status='importing',
            origin_station=manifest.get('origin_station', ''),
            source_fingerprint=manifest['fingerprints'][0]['fingerprint']
            if manifest.get('fingerprints') else '',
            actor=user.email)
        db.add(registration)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            rows = db.scalars(select(MigrationImport).where(
                MigrationImport.package_digest == package_digest)).all()
            if any(x.status == 'succeeded' for x in rows):
                raise ImportDuplicate(next(x for x in rows if x.status == 'succeeded'))
            if any(x.status == 'importing' for x in rows):
                # 另一并发请求仍在导入中：让调用方稍后重试，绝不并行落两份。
                raise HTTPException(409, '同一封存包正在另一会话导入，请稍后重试')
            # 仅剩 failed 历史：失败可重试，新建登记由部分唯一索引放行。
            db.add(registration)
            db.commit()
        registration_id = registration.id
    finally:
        db.close()

    # 阶段二：全部业务写入在单个事务内完成；任何异常整体回滚，登记标记 failed。
    db = SessionLocal()
    staging_paths: list[Path] = []
    try:
        _, dataset_meta, windows, runs, artifacts, policies, reviews = parsed
        user_emails = {u.email: u for u in db.scalars(select(User))}

        existing_dataset = db.scalar(select(WindowDataset).where(
            WindowDataset.name == dataset_meta['name']))
        isolated = False
        if existing_dataset:
            existing_rows = [
                {'reference': w.reference, 'features': w.features, 'label': w.label}
                for w in db.scalars(select(SensorWindow).where(
                    SensorWindow.dataset_id == existing_dataset.id))]
            if window_digest(existing_rows) == manifest['window_digest']:
                target_dataset = existing_dataset
                dataset_mode = 'reused_same_content'
            else:
                # 同名异值：隔离保存，绝不覆盖目标原有研究。
                isolated = True
                dataset_mode = 'isolated_name_collision'
                origin_tag = _SAFE_NAME.sub('', manifest.get('origin_station', ''))[:30]
                base = '%s（异地迁入·%s）' % (dataset_meta['name'], origin_tag or '来源不明')
                target_dataset = WindowDataset(
                    name=_unique_dataset_name(db, base),
                    filename=dataset_meta['filename'],
                    description=(dataset_meta.get('description', '') +
                                 ' [异地封存迁入，原名 %s，来源 %s]' % (
                                     dataset_meta['name'],
                                     manifest.get('origin_station', '未知')))[:500])
                db.add(target_dataset)
                db.flush()
        else:
            dataset_mode = 'created'
            target_dataset = WindowDataset(
                name=dataset_meta['name'], filename=dataset_meta['filename'],
                description=dataset_meta.get('description', ''))
            db.add(target_dataset)
            db.flush()

        if dataset_mode != 'reused_same_content':
            db.add_all([
                SensorWindow(dataset_id=target_dataset.id, reference=w['reference'],
                             features=w['features'], label=w['label'])
                for w in windows])
            db.flush()

        target_windows = db.scalars(select(SensorWindow).where(
            SensorWindow.dataset_id == target_dataset.id)).all()
        ref_to_target = {w.reference: w.id for w in target_windows}
        window_id_map: dict[int, int] = {}
        for w in windows:
            target_id = ref_to_target.get(w['reference'])
            if target_id is None:
                raise ImportRejected('窗口 %s 在目标数据集缺失' % w['reference'])
            window_id_map[w['source_window_id']] = target_id

        run_id_map: dict[int, int] = {}
        run_origins: dict[int, dict] = {}
        target_results: dict[int, dict] = {}
        for run in runs:
            new_result = remap_run_result(run['result'], window_id_map)
            obj = TrainingRun(
                dataset_id=target_dataset.id, name=run['name'],
                status=RunStatus.SUCCEEDED, config=run['config'],
                result=new_result, error=None,
                creator_id=user.id,
                created_at=datetime.fromisoformat(run['created_at']))
            db.add(obj)
            db.flush()
            run_id_map[run['source_run_id']] = obj.id
            target_results[obj.id] = new_result
            run_origins[obj.id] = {
                'source_run_id': run['source_run_id'],
                'source_fingerprint': run['result']['fingerprint'],
            }

        policy_id_map: dict[int, int] = {}
        for policy in policies:
            creator = user_emails.get(policy.get('creator_email', ''), user)
            obj = ThresholdPolicy(
                run_id=run_id_map[policy['source_run_id']], name=policy['name'],
                threshold=policy['threshold'], status=policy['status'],
                note=policy['note'], creator_id=creator.id,
                created_at=datetime.fromisoformat(policy['created_at']))
            db.add(obj)
            db.flush()
            policy_id_map[policy['source_policy_id']] = obj.id

        review_id_map: dict[str, int] = {}
        for review in reviews:
            reviewer = user_emails.get(review.get('reviewer_email', ''), user)
            obj = InspectionReview(
                policy_id=policy_id_map[review['source_policy_id']],
                window_id=window_id_map[review['source_window_id']],
                verdict=review['verdict'], note=review['note'],
                reviewer_id=reviewer.id,
                created_at=datetime.fromisoformat(review['created_at']))
            db.add(obj)
            db.flush()
            review_id_map['%s:%s' % (review['source_policy_id'],
                                     review['source_window_id'])] = obj.id

        # 训练文件先写暂存名（同卷），事务提交后再原子改名，避免半成品可见。
        artifact_dir = Path(settings.ARTIFACT_DIR)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for run in runs:
            target_run_id = run_id_map[run['source_run_id']]
            payload = json.dumps(target_results[target_run_id], ensure_ascii=False,
                                 allow_nan=False).encode('utf-8')
            fd, tmp_name = tempfile.mkstemp(
                prefix='.staging-training-%s-' % target_run_id, dir=artifact_dir)
            staging = Path(tmp_name)
            staging_paths.append(staging)
            with os.fdopen(fd, 'wb') as fh:
                fh.write(payload)

        summary = {
            'dataset_id': target_dataset.id,
            'dataset_name': target_dataset.name,
            'dataset_mode': dataset_mode,
            'isolated': isolated,
            'window_count': len(windows),
            'window_id_map': {str(k): v for k, v in window_id_map.items()},
            'run_id_map': {str(k): v for k, v in run_id_map.items()},
            'policy_id_map': {str(k): v for k, v in policy_id_map.items()},
            'review_id_map': review_id_map,
            'run_origins': {str(k): v for k, v in run_origins.items()},
            'source_fingerprints': {
                str(run_id_map[r['source_run_id']]): r['result']['fingerprint']
                for r in runs},
            'origin_station': manifest.get('origin_station', ''),
            'exported_at': manifest.get('exported_at'),
            'counts': {'runs': len(runs), 'policies': len(policies),
                       'reviews': len(reviews)},
        }
        registration = db.get(MigrationImport, registration_id)
        registration.summary = summary

        # 单事务提交：提交成功前，任何业务行都不可见，暂存文件也不在正式位置。
        db.commit()
    except Exception as exc:
        db.rollback()
        for staging in staging_paths:
            try:
                staging.unlink()
            except OSError:
                pass
        _mark_failed(registration_id, exc)
        if isinstance(exc, HTTPException):
            raise
        raise ImportRejected('迁入失败，已整包回滚: %s' % exc) from exc
    finally:
        db.close()

    # 阶段三：事务已提交，原子公布训练文件。
    # 公布失败时先按库内结果重建文件：恢复一致则补登成功；
    # 存储彻底不可用则补偿删除本次业务行，保证“均可用或均不可见”且重试不产生副本。
    db = SessionLocal()
    try:
        for staging in staging_paths:
            target_run_id = int(staging.name.split('-')[2])
            final_path = artifact_dir / f'training-{target_run_id}.json'
            os.replace(staging, final_path)
        registration = db.get(MigrationImport, registration_id)
        registration.status = 'succeeded'
        registration.finished_at = datetime.now(timezone.utc)
        db.commit()
        record(db, user.email, 'migration.imported', {
            'package_digest': package_digest,
            'dataset_id': registration.summary.get('dataset_id'),
            'dataset_mode': registration.summary.get('dataset_mode'),
            'runs': registration.summary['counts']['runs'],
            'policies': registration.summary['counts']['policies'],
            'reviews': registration.summary['counts']['reviews'],
        })
        return _serialize_registration(registration)
    except OSError as exc:
        db.rollback()
        outcome = _finalize_or_compensate(registration_id, user,
                                          '训练文件公布失败: %s' % exc)
        if outcome == 'succeeded':
            with SessionLocal() as check_db:
                return _serialize_registration(check_db.get(MigrationImport, registration_id))
        raise ImportRejected('训练文件公布失败且无法恢复，已整包回滚，请重试导入') from exc
    finally:
        db.close()


def _mark_failed(registration_id: int, exc: Exception):
    db = SessionLocal()
    try:
        registration = db.get(MigrationImport, registration_id)
        if registration and registration.status != 'succeeded':
            registration.status = 'failed'
            registration.error = str(exc)[:1000]
            registration.finished_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()


def _artifacts_consistent(registration: MigrationImport, db) -> bool:
    """登记内全部目标运行的训练文件都存在且与库内结果逐键一致。"""
    artifact_dir = Path(settings.ARTIFACT_DIR)
    run_map = (registration.summary or {}).get('run_id_map', {})
    for target_id in run_map.values():
        run = db.get(TrainingRun, target_id)
        if not run:
            return False
        final_path = artifact_dir / f'training-{target_id}.json'
        if not final_path.exists():
            return False
        try:
            if json.loads(final_path.read_text(encoding='utf-8')) != run.result:
                return False
        except (OSError, json.JSONDecodeError):
            return False
    return True


def _rebuild_artifacts(registration: MigrationImport, db) -> bool:
    """按库内已提交结果重建训练文件；存储不可用时返回 False。"""
    artifact_dir = Path(settings.ARTIFACT_DIR)
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        run_map = (registration.summary or {}).get('run_id_map', {})
        for target_id in run_map.values():
            run = db.get(TrainingRun, target_id)
            if not run:
                return False
            final_path = artifact_dir / f'training-{target_id}.json'
            if not final_path.exists():
                final_path.write_text(json.dumps(
                    run.result, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    except OSError:
        return False
    return _artifacts_consistent(registration, db)


def _compensate_business_rows(registration: MigrationImport, db):
    """删除本次导入写入的全部业务行（倒序），让研究整体回到不可见状态。

    reused_same_content 模式下数据集与窗口是迁入前已存在的，绝不删除。
    """
    summary = registration.summary or {}
    for target_review_ids in [summary.get('review_id_map', {})]:
        for target_id in target_review_ids.values():
            obj = db.get(InspectionReview, target_id)
            if obj:
                db.delete(obj)
    for target_id in summary.get('policy_id_map', {}).values():
        obj = db.get(ThresholdPolicy, target_id)
        if obj:
            db.delete(obj)
    for target_id in summary.get('run_id_map', {}).values():
        obj = db.get(TrainingRun, target_id)
        if obj:
            db.delete(obj)
    if summary.get('dataset_mode') != 'reused_same_content':
        dataset = db.get(WindowDataset, summary.get('dataset_id'))
        if dataset:
            for w in db.scalars(select(SensorWindow).where(
                    SensorWindow.dataset_id == dataset.id)).all():
                db.delete(w)
            db.delete(dataset)
    artifact_dir = Path(settings.ARTIFACT_DIR)
    for target_id in summary.get('run_id_map', {}).values():
        try:
            (artifact_dir / f'training-{target_id}.json').unlink()
        except OSError:
            pass


def _finalize_or_compensate(registration_id: int, user, reason: str) -> str:
    """阶段三失败后的统一收尾，返回 'succeeded' 或 'failed'。"""
    db = SessionLocal()
    try:
        registration = db.get(MigrationImport, registration_id)
        if not registration:
            return 'failed'
        if _rebuild_artifacts(registration, db) and _artifacts_consistent(registration, db):
            # 库与文件同时可用：补登成功，导入仍然只落一份。
            registration.status = 'succeeded'
            registration.error = None
            registration.finished_at = datetime.now(timezone.utc)
            db.commit()
            record(db, user.email, 'migration.imported', {
                'package_digest': registration.package_digest,
                'dataset_id': registration.summary.get('dataset_id'),
                'dataset_mode': registration.summary.get('dataset_mode'),
                'recovered': True,
            })
            return 'succeeded'
        # 存储彻底不可用：补偿删除业务行，让整包回到均不可见，可安全重试。
        db.rollback()
        registration = db.get(MigrationImport, registration_id)
        _compensate_business_rows(registration, db)
        registration.status = 'failed'
        registration.error = reason[:1000]
        registration.finished_at = datetime.now(timezone.utc)
        db.commit()
        record(db, user.email, 'migration.import_rolled_back', {
            'package_digest': registration.package_digest, 'reason': reason[:500]})
        return 'failed'
    finally:
        db.close()


def recover_pending_imports():
    """启动时调用：回收中断的导入登记与暂存文件。

    - 业务事务未提交：业务行本就不可见，清理暂存并标记 failed（可重试）；
    - 业务事务已提交但文件未公布：重建文件并补登 succeeded；
      若存储不可用则补偿删除业务行，保持“均可用或均不可见”；
    - 清理所有 .staging- 暂存文件；
    - 对 succeeded 登记补齐缺失训练文件。
    """
    db = SessionLocal()
    try:
        pending = db.scalars(select(MigrationImport).where(
            MigrationImport.status == 'importing')).all()
        for registration in pending:
            run_map = (registration.summary or {}).get('run_id_map', {})
            business_visible = any(
                db.get(TrainingRun, rid) is not None for rid in run_map.values())
            if business_visible:
                if _rebuild_artifacts(registration, db) and \
                        _artifacts_consistent(registration, db):
                    registration.status = 'succeeded'
                    registration.error = None
                    registration.finished_at = datetime.now(timezone.utc)
                else:
                    _compensate_business_rows(registration, db)
                    registration.status = 'failed'
                    registration.error = '导入中断且文件存储不可用，已整包回滚，可重试'
                    registration.finished_at = datetime.now(timezone.utc)
            else:
                registration.status = 'failed'
                registration.error = '导入过程中断，业务事务未提交，可重新导入'
                registration.finished_at = datetime.now(timezone.utc)
        db.commit()

        succeeded = db.scalars(select(MigrationImport).where(
            MigrationImport.status == 'succeeded')).all()
        for registration in succeeded:
            if not _artifacts_consistent(registration, db):
                _rebuild_artifacts(registration, db)
    finally:
        db.close()

    artifact_dir = Path(settings.ARTIFACT_DIR)
    if artifact_dir.exists():
        for stale in artifact_dir.glob('.staging-*'):
            try:
                stale.unlink()
            except OSError:
                pass


def _serialize_registration(obj: MigrationImport) -> dict:
    return {
        'id': obj.id,
        'package_digest': obj.package_digest,
        'status': obj.status,
        'origin_station': obj.origin_station,
        'source_fingerprint': obj.source_fingerprint,
        'summary': obj.summary,
        'error': obj.error,
        'actor': obj.actor,
        'created_at': obj.created_at.isoformat() if obj.created_at else None,
        'finished_at': obj.finished_at.isoformat() if obj.finished_at else None,
    }


def list_imports(db) -> list[dict]:
    return [_serialize_registration(x) for x in db.scalars(
        select(MigrationImport).order_by(MigrationImport.id.desc()))]


def get_import(db, import_id: int) -> MigrationImport:
    obj = db.get(MigrationImport, import_id)
    if not obj:
        raise HTTPException(404, '迁入记录不存在')
    return obj
