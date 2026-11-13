import { useEffect, useState } from 'react';
import { api, type Bootstrap, type ProbeResult } from './api';
import type { Notify } from './App';

interface Props {
  boot: Bootstrap;
  refresh: () => Promise<void>;
  notify: Notify;
}

type Action = 'enable' | 'disable' | 'delete';

export default function SourcesPage({ boot, refresh, notify }: Props) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [probe, setProbe] = useState<Record<string, ProbeResult>>({});
  const [showAdd, setShowAdd] = useState(false);
  const [showProbe, setShowProbe] = useState(false);
  const [keyword, setKeyword] = useState('');
  const [busy, setBusy] = useState('');
  const [form, setForm] = useState({ name: '', key: '', api: '', detail: '' });
  const [editing, setEditing] = useState<{
    id: string; name: string; key: string; api: string; detail: string;
  } | null>(null);

  useEffect(() => {
    const valid = new Set(boot.sources.map((s) => s.id));
    setSelected((prev) => new Set([...prev].filter((id) => valid.has(id))));
  }, [boot]);

  async function run(key: string, task: () => Promise<void>) {
    setBusy(key);
    try {
      await task();
    } catch (err) {
      notify(err instanceof Error ? err.message : '操作失败', 'err');
    } finally {
      setBusy('');
    }
  }

  function toggleSelect(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    setSelected((prev) =>
      prev.size === boot.sources.length
        ? new Set()
        : new Set(boot.sources.map((s) => s.id)),
    );
  }

  async function batch(action: Action) {
    if (!selected.size) {
      notify('请先选择视频源', 'err');
      return;
    }
    if (action === 'delete'
      && !window.confirm(`确定删除选中的 ${selected.size} 个视频源？`)) return;
    await run(`batch-${action}`, async () => {
      const result = await api.batchSources([...selected], action);
      notify(`已${action === 'delete' ? '删除' : action === 'enable' ? '启用' : '禁用'} ${result.changed} 个视频源`, 'ok');
      setSelected(new Set());
      await refresh();
    });
  }

  async function doProbe() {
    const word = keyword.trim();
    if (!word) {
      notify('请输入检测关键词', 'err');
      return;
    }
    await run('probe', async () => {
      const ids = selected.size ? [...selected] : [];
      const result = await api.probeSources(ids, word);
      setProbe((prev) => {
        const next = { ...prev };
        result.results.forEach((r) => { next[r.id] = r; });
        return next;
      });
      const ok = result.results.filter((r) => r.ok).length;
      const tail = result.timed_out ? `，${result.timed_out} 个超时未检测` : '';
      notify(`检测完成：${ok}/${result.done} 个可用${tail}（${result.elapsed_ms} ms）`, 'ok');
      setShowProbe(false);
    });
  }

  async function toggleOne(id: string, enabled: boolean) {
    await run(`toggle-${id}`, async () => {
      await api.updateSource(id, { enabled });
      await refresh();
    });
  }

  async function removeOne(id: string, name: string) {
    if (!window.confirm(`确定删除「${name}」？`)) return;
    await run(`del-${id}`, async () => {
      await api.deleteSource(id);
      notify('已删除', 'ok');
      await refresh();
    });
  }

  async function doAdd() {
    await run('add', async () => {
      if (!form.api.trim()) throw new Error('请填写 API 地址');
      await api.addSource({
        name: form.name.trim(), key: form.key.trim(),
        api: form.api.trim(), detail: form.detail.trim(),
      });
      setForm({ name: '', key: '', api: '', detail: '' });
      setShowAdd(false);
      notify('视频源已添加，默认未启用', 'ok');
      await refresh();
    });
  }

  async function doSaveEdit() {
    if (!editing) return;
    await run('save-edit', async () => {
      await api.updateSource(editing.id, {
        name: editing.name.trim(), key: editing.key.trim(),
        api: editing.api.trim(), detail: editing.detail.trim(),
      });
      setEditing(null);
      notify('视频源已更新', 'ok');
      await refresh();
    });
  }

  const field = (key: keyof typeof form, label: string, placeholder: string) => (
    <div className="grow">
      <label className="field">{label}</label>
      <input
        type="text" value={form[key]} placeholder={placeholder}
        onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.value }))}
      />
    </div>
  );

  return (
    <>
      <div className="page-head">
        <h1>片源配置</h1>
        <p className="page-desc">
          管理已导入或手工添加的视频源；有效性检测只在点击时触发，不会自动探活。
        </p>
      </div>

      <section className="card">
        <div className="toolbar">
          <h4 className="card-title" style={{ margin: 0 }}>视频源列表</h4>
          <span className="small">已选择 {selected.size} 个视频源</span>
        </div>
        <div className="toolbar-actions">
          {selected.size > 0 && (
            <>
              <button className="btn sm success" disabled={busy === 'batch-enable'}
                      onClick={() => void batch('enable')}>批量启用</button>
              <button className="btn sm warning" disabled={busy === 'batch-disable'}
                      onClick={() => void batch('disable')}>批量禁用</button>
              <button className="btn sm danger" disabled={busy === 'batch-delete'}
                      onClick={() => void batch('delete')}>批量删除</button>
            </>
          )}
          <button className="btn sm info"
                  onClick={() => { setKeyword(''); setShowProbe(true); }}>
            有效性检测
          </button>
          <button className="btn sm primary" onClick={() => setShowAdd(!showAdd)}>
            添加视频源
          </button>
          {selected.size === 0 && (
            <span className="small">勾选视频源后可进行批量操作</span>
          )}
        </div>

        {showProbe && (
          <div className="modal-overlay" onClick={() => setShowProbe(false)}>
            <div className="modal" onClick={(e) => e.stopPropagation()}>
              <div className="modal-title">视频源有效性检测</div>
              <p className="card-sub">请输入检测用的搜索关键词</p>
              <input
                type="text" value={keyword} placeholder="请输入搜索关键词"
                autoFocus onChange={(e) => setKeyword(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') void doProbe(); }}
              />
              <div className="modal-actions">
                <span className="small">
                  {selected.size
                    ? `将检测选中的 ${selected.size} 个（最长 12 秒）`
                    : `将检测全部 ${boot.sources.length} 个（最长 12 秒）`}
                </span>
                <button className="btn ghost" onClick={() => setShowProbe(false)}>取消</button>
                <button className="btn primary" disabled={!keyword.trim() || busy === 'probe'}
                        onClick={() => void doProbe()}>
                  {busy === 'probe' ? '检测中…' : '开始检测'}
                </button>
              </div>
            </div>
          </div>
        )}

        {showAdd && (
          <div className="stack probe-box mt">
            <div className="row">
              {field('name', '名称', '例如：示例资源')}
              {field('key', 'KEY', '例如：example.com')}
            </div>
            <div className="row">
              {field('api', 'API 地址', '苹果 CMS 采集接口')}
              {field('detail', 'DETAIL 地址', '站点主页（可选）')}
            </div>
            <div className="row end">
              <button className="btn primary sm" disabled={busy === 'add'}
                      onClick={() => void doAdd()}>添加</button>
            </div>
          </div>
        )}

        {editing && (
          <div className="modal-overlay" onClick={() => setEditing(null)}>
            <div className="modal" onClick={(e) => e.stopPropagation()}>
              <div className="modal-title">编辑视频源</div>
              <p className="card-sub">
                改完保存即时生效。仅改名称/KEY/DETAIL 时不影响已授权关系；
                API 地址可改，但该源的 ID 不会随之变化。
              </p>
              <div className="stack">
                <div>
                  <label className="field">名称</label>
                  <input type="text" value={editing.name}
                         onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
                </div>
                <div>
                  <label className="field">KEY</label>
                  <input type="text" value={editing.key}
                         onChange={(e) => setEditing({ ...editing, key: e.target.value })} />
                </div>
                <div>
                  <label className="field">API 地址</label>
                  <input type="text" value={editing.api}
                         onChange={(e) => setEditing({ ...editing, api: e.target.value })} />
                </div>
                <div>
                  <label className="field">DETAIL 地址（可选）</label>
                  <input type="text" value={editing.detail}
                         onChange={(e) => setEditing({ ...editing, detail: e.target.value })} />
                </div>
              </div>
              <div className="modal-actions">
                <button className="btn ghost" onClick={() => setEditing(null)}>取消</button>
                <button className="btn primary"
                        disabled={busy === 'save-edit' || !editing.api.trim()}
                        onClick={() => void doSaveEdit()}>
                  {busy === 'save-edit' ? '保存中…' : '保存'}
                </button>
              </div>
            </div>
          </div>
        )}

        {boot.sources.length === 0 ? (
          <div className="empty mt">
            <div className="empty-icon">▦</div>
            <h2>暂无视频源</h2>
            <p>前往「订阅配置」拉取一份配置，或点击上方「添加视频源」手动添加。</p>
          </div>
        ) : (
          <div className="table-wrap stack-table mt">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 38 }}>
                    <input className="check" type="checkbox"
                           checked={selected.size === boot.sources.length && selected.size > 0}
                           onChange={toggleAll} />
                  </th>
                  <th>名称</th>
                  <th>KEY</th>
                  <th>API 地址</th>
                  <th>DETAIL 地址</th>
                  <th>状态</th>
                  <th>有效性</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {boot.sources.map((s) => {
                  const result = probe[s.id];
                  return (
                    <tr key={s.id}>
                      <td>
                        <input className="check" type="checkbox"
                               checked={selected.has(s.id)}
                               onChange={() => toggleSelect(s.id)} />
                      </td>
                      <td data-label="名称">{s.name}</td>
                      <td className="api" data-label="KEY">{s.key || '—'}</td>
                      <td className="api" data-label="API" title={s.api}>{s.api}</td>
                      <td className="api" data-label="DETAIL" title={s.detail || ''}>{s.detail || '—'}</td>
                      <td data-label="状态">
                        <span className={`badge ${s.enabled ? 'ok' : 'warn'}`}>
                          {s.enabled ? '启用中' : '已禁用'}
                        </span>
                      </td>
                      <td data-label="有效性">
                        {!result ? <span className="small">未检测</span>
                          : result.ok
                            ? <span className="badge ok">{result.count} 条 · {result.elapsed_ms ?? 0}ms</span>
                            : <span className="badge danger">不可用</span>}
                      </td>
                      <td data-label="操作">
                        <div className="row">
                          <button className="btn sm ghost"
                                  onClick={() => setEditing({
                                    id: s.id, name: s.name, key: s.key || '',
                                    api: s.api, detail: s.detail || '',
                                  })}>编辑</button>
                          <button className="btn sm" disabled={busy === `toggle-${s.id}`}
                                  onClick={() => void toggleOne(s.id, !s.enabled)}>
                            {s.enabled ? '禁用' : '启用'}
                          </button>
                          <button className="btn sm danger"
                                  onClick={() => void removeOne(s.id, s.name)}>删除</button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <div className="card-title">统计</div>
        <div className="pill-row mt">
          <span className="badge">共 {boot.sources_total} 个视频源</span>
          <span className="badge ok">启用 {boot.sources_enabled} 个</span>
          {boot.sources.some((s) => s.adult) && (
            <span className="pill">特殊标记 {boot.sources.filter((s) => s.adult).length} 个（默认禁用）</span>
          )}
          <span className="pill">来源：订阅 / 手动</span>
          <span className="pill">参与搜索上限 {boot.config.enabled_limit}</span>
        </div>
        <p className="hint">
          订阅配置页的全量拉取会替换订阅来源的源，手工添加的源始终保留。
          搜索使用固定并发与总预算，源较多时可在「搜索播放」页调整。
        </p>
      </section>
    </>
  );
}
