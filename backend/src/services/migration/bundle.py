"""封存研究包（research bundle）的规范编码与安全 ZIP 读写。

包内布局：
  manifest.json
  dataset.json
  windows.json
  runs/run-{source_id}.json
  artifacts/training-{source_id}.json
  policies.json
  reviews.json

完整性采用两级 SHA-256：
  1. 每个成员按原始字节摘要，记入 manifest.files；
  2. package_digest 覆盖全部非 manifest 成员，manifest_digest 覆盖清单规范 JSON。
摘要只依赖成员内容，与压缩参数/ZIP 元数据无关，因此可在任意机器复核。
"""
import hashlib
import io
import json
import zipfile

BUNDLE_VERSION = 'research-bundle-v1'
MANIFEST_NAME = 'manifest.json'
_ALGORITHM = 'logistic-pipeline-v2-sentinel'


class BundleError(ValueError):
    """封存包结构、哈希或交叉引用校验失败。"""


def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_json(obj) -> str:
    return digest_bytes(canonical_json(obj))


def safe_member_name(name: str) -> str:
    if not name or name.startswith('/') or '\\' in name or '..' in name.split('/'):
        raise BundleError('封存包含非法路径成员: %r' % name)
    return name


def write_bundle(members: dict[str, bytes]) -> tuple[bytes, dict]:
    """members: 成员路径 -> 原始字节（manifest 之外的全部成员）。

    返回 zip 字节与最终 manifest（manifest 由调用方先传入，键为 MANIFEST_NAME 时
    会被本函数重算后的版本替换）。
    """
    manifest = json.loads(members[MANIFEST_NAME].decode('utf-8'))
    files = {}
    for path in sorted(p for p in members if p != MANIFEST_NAME):
        files[safe_member_name(path)] = digest_bytes(members[path])
    manifest['files'] = files
    # 包摘要只依赖业务成员内容，不依赖 manifest 自身，避免循环。
    package_digest = digest_bytes(''.join(
        '%s:%s\n' % (p, files[p]) for p in sorted(files)).encode('utf-8'))
    manifest['package_digest'] = package_digest
    manifest.pop('manifest_digest', None)
    manifest_digest = digest_json(manifest)
    manifest['manifest_digest'] = manifest_digest
    manifest_bytes = canonical_json(manifest)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME, manifest_bytes)
        for path in sorted(p for p in members if p != MANIFEST_NAME):
            zf.writestr(safe_member_name(path), members[path])
    return buffer.getvalue(), manifest


def read_bundle(data: bytes) -> tuple[dict, dict[str, bytes]]:
    """校验 ZIP 结构与两级哈希，返回 (manifest, {path: bytes})。

    任何篡改、缺件、多余成员或摘要不符都抛 BundleError，调用方必须整包拒收。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BundleError('不是有效的封存 ZIP 包') from exc
    members: dict[str, bytes] = {}
    with zf:
        bad = zf.testzip()
        if bad is not None:
            raise BundleError('封存包压缩数据损坏: %s' % bad)
        for info in zf.infolist():
            name = safe_member_name(info.filename)
            if info.is_dir():
                raise BundleError('封存包不允许目录成员: %s' % name)
            members[name] = zf.read(info)
    if MANIFEST_NAME not in members:
        raise BundleError('封存包缺少 manifest.json')
    try:
        manifest = json.loads(members[MANIFEST_NAME].decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError('manifest.json 不是合法 UTF-8 JSON') from exc

    declared_files = manifest.get('files')
    if not isinstance(declared_files, dict):
        raise BundleError('manifest 缺少 files 摘要表')
    actual = {p: digest_bytes(b) for p, b in members.items() if p != MANIFEST_NAME}
    if set(actual) != set(declared_files):
        raise BundleError('封存包成员清单与 manifest.files 不一致（缺件或夹带文件）')
    for path, want in declared_files.items():
        if actual.get(path) != want:
            raise BundleError('成员摘要不符，封存包可能被篡改: %s' % path)

    want_package = manifest.get('package_digest')
    got_package = digest_bytes(''.join(
        '%s:%s\n' % (p, actual[p]) for p in sorted(actual)).encode('utf-8'))
    if want_package != got_package:
        raise BundleError('package_digest 不符，封存包可能被篡改')

    want_manifest = manifest.get('manifest_digest')
    manifest_copy = {k: v for k, v in manifest.items() if k != 'manifest_digest'}
    if want_manifest != digest_json(manifest_copy):
        raise BundleError('manifest_digest 不符，清单可能被篡改')
    if manifest.get('bundle_version') != BUNDLE_VERSION:
        raise BundleError('封存包版本不受支持: %r' % manifest.get('bundle_version'))
    return manifest, members


def json_member(members: dict[str, bytes], path: str):
    if path not in members:
        raise BundleError('封存包缺少必需成员: %s' % path)
    try:
        return json.loads(members[path].decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError('成员不是合法 UTF-8 JSON: %s' % path) from exc


def window_digest(windows: list[dict]) -> str:
    """窗口内容摘要：按 reference 稳定排序，覆盖六特征与 label，不含数据库 id。"""
    from src.constants.features import FEATURES
    canonical = [{
        'reference': w['reference'],
        'features': {k: float(w['features'][k]) for k in sorted(FEATURES)},
        'label': int(w['label']),
    } for w in sorted(windows, key=lambda w: w['reference'])]
    return digest_json(canonical)


def algorithm_tag() -> str:
    return _ALGORITHM
