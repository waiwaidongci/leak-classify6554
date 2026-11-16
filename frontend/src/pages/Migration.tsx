import {useCallback, useEffect, useRef, useState} from 'react';
import type {Dataset, User, MigrationImport, MigrationVerify} from '../types/domain';
import {migrationApi} from '../api/migration';
import {PageTitle} from '../components/common/PageTitle';
import {Badge} from '../components/common/Badge';
import {Notice} from '../components/common/Notice';
import {Table} from '../components/common/Table';
import {Empty} from '../components/common/Empty';
import {useAsync} from '../hooks/useAsync';
import {date} from '../utils/format';

const MODE_TEXT: Record<string, string> = {
  created: '新建数据集',
  isolated_name_collision: '同名异值 · 隔离保存',
  reused_same_content: '同名同值 · 复用既有数据集',
};

export function MigrationPage({datasets, user, reloadData}:
  {datasets: Dataset[]; user: User; reloadData: () => Promise<any>}) {
  const [imports, setImports] = useState<MigrationImport[]>([]);
  const [verifies, setVerifies] = useState<Record<number, MigrationVerify>>({});
  const fileRef = useRef<HTMLInputElement>(null);
  const {busy, error, message, act} = useAsync();

  const reloadImports = useCallback(async () => {
    setImports(await migrationApi.imports());
  }, []);
  useEffect(() => {reloadImports().catch(() => undefined);}, [reloadImports]);

  async function runVerify(id: number) {
    const report = await migrationApi.verify(id);
    setVerifies((prev) => ({...prev, [id]: report}));
  }

  return <>
    <PageTitle index="07 / RESEARCH TRANSFER"
      title="封存研究异地迁出迁入，引用按实际窗口重建。"
      description="连同输入窗口、训练文件、阈值策略与人工复核整包搬迁，不触发重训。篡改、缺件或交叉引用不符时整包拒收；同名异值数据集隔离保存，目标原有研究保持原样。"
      action={user.role !== 'viewer' && <button onClick={() => fileRef.current?.click()}>导入封存包 ↗</button>}/>
    <Notice error={error} message={message}/>
    <input ref={fileRef} type="file" accept=".zip,application/zip" style={{display: 'none'}}
      onChange={(e) => {
        const file = e.target.files?.[0];
        e.target.value = '';
        if (!file) return;
        act(async () => {
          const result = await migrationApi.importPackage(file);
          await Promise.all([reloadImports(), reloadData()]);
          await runVerify(result.id);
          const mode = result.summary ? MODE_TEXT[result.summary.dataset_mode] || result.summary.dataset_mode : '';
          return `封存包已迁入：${result.summary?.dataset_name || ''}（${mode}）`;
        }, '封存包已迁入');
      }}/>

    <div className="section-title">
      <h2>一、迁出封存研究</h2>
      <small>仅分析师与负责人可操作 · 只打包已成功运行</small>
    </div>
    {!datasets.length
      ? <Empty title="暂无可迁出的数据集" help="先在窗口样本导入窗口并完成一次训练。"/>
      : <Table headers={['数据集', '窗口数', '迁出动作', '最近包摘要']}>
          {datasets.map((d) => <tr key={d.id}>
            <td><strong>{d.name}</strong><small>{d.positive} 阳性 / {d.count} 窗口</small></td>
            <td>{d.count}</td>
            <td>
              {user.role === 'viewer'
                ? <span className="mono">观察员仅可查看</span>
                : <button className="text-button" disabled={busy}
                    onClick={() => act(async () => {
                      const {digest} = await migrationApi.exportBundle(d.id);
                      return `已生成封存包，包摘要 ${digest.slice(0, 16)}…`;
                    }, '封存包已开始下载')}>
                    打包并下载 ZIP ↗
                  </button>}
            </td>
            <td className="mono detail">SHA-256 随响应头 X-Package-Digest 返回</td>
          </tr>)}
        </Table>}

    <div className="section-title">
      <h2>二、迁入登记与核验</h2>
      <small>同包并发只落一份 · 失败可重试</small>
    </div>
    {!imports.length
      ? <Empty title="尚无迁入记录" help="点击右上角“导入封存包”，选择异地生成的 research-bundle ZIP。"/>
      : <Table headers={['时间', '来源站 / 数据集', '落位方式', '计数', '状态 / 核验', '操作']}>
          {imports.map((item) => {
            const v = verifies[item.id];
            return <tr key={item.id}>
              <td className="mono">{date(item.created_at)}</td>
              <td>
                <strong>{item.summary?.dataset_name || '—'}</strong>
                <small>{item.origin_station || '未知来源'} · 包 {item.package_digest.slice(0, 18)}…</small>
              </td>
              <td>{item.summary ? (MODE_TEXT[item.summary.dataset_mode] || item.summary.dataset_mode) : '—'}</td>
              <td>
                {item.summary
                  ? `${item.summary.counts.runs} 运行 / ${item.summary.counts.policies} 策略 / ${item.summary.counts.reviews} 复核`
                  : '—'}
              </td>
              <td>
                <Badge status={item.status}/>
                {v && <small className={v.all_ok ? 'verify-ok' : 'verify-bad'}>
                  {v.all_ok ? `核验通过：${v.reports.length} 个运行指纹与逐窗判别一致` : '核验存在不符项'}
                </small>}
                {item.error && <small className="verify-bad">{item.error}</small>}
              </td>
              <td>
                <button className="text-button" disabled={busy || item.status !== 'succeeded'}
                  onClick={() => act(() => runVerify(item.id), '核验完成')}>
                  追溯核验 ↗
                </button>
              </td>
            </tr>
          })}
        </Table>}

    {Object.values(verifies).filter(Boolean).map((v) => <section key={v.import_id} className="verify-panel">
      <div className="section-title">
        <h2>核验明细 / 包 {v.package_digest.slice(0, 16)}…</h2>
        <small>{v.origin_station}</small>
      </div>
      <Table headers={['迁入运行', '原指纹（重算一致）', '逐窗判别', '分割计数', '结论']}>
        {v.reports.map((r) => <tr key={r.run_id}>
          <td><strong>{r.run_name}</strong><small>运行 #{r.run_id}</small></td>
          <td className="mono detail">
            {r.fingerprint.slice(0, 24)}…
            <small>重算 {r.fingerprint_recomputed.slice(0, 24)}…</small>
          </td>
          <td>{r.prediction_count} 条留出判别</td>
          <td className="mono">
            {Object.entries(r.split_counts || {}).map(([k, c]) => `${k}:${c.n}`).join(' / ')}
          </td>
          <td>
            {r.ok
              ? <Badge status="confirmed"/>
              : <><Badge status="failed"/><small className="verify-bad">{r.problems.join('；')}</small></>}
          </td>
        </tr>)}
      </Table>
    </section>)}

    <div className="section-title">
      <h2>封存包口径</h2>
      <small>BUNDLE CONTRACT</small>
    </div>
    <div className="feature-index">
      <div><b>01</b><span>两级 SHA-256<small>成员摘要表 + package_digest，与压缩参数无关，可任意机器复核</small></span></div>
      <div><b>02</b><span>引用全部重建<small>源库数字 id 不沿用，split_ids 与逐窗判别按目标窗口主键重写</small></span></div>
      <div><b>03</b><span>原指纹保留<small>训练输入指纹随包封存，迁入后按实际窗口重算比对</small></span></div>
      <div><b>04</b><span>不重训<small>运行直接以 succeeded 落库，训练文件与库内结果逐键一致</small></span></div>
      <div><b>05</b><span>整包拒收<small>篡改、缺件、路径穿越或交叉引用不符，任一失败不写业务数据</small></span></div>
      <div><b>06</b><span>原子可见<small>数据库事务提交后训练文件才改名公布，中断后自动恢复或回滚</small></span></div>
    </div>
  </>;
}
