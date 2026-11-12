import { useCallback, useEffect, useState } from 'react';
import {
  api, type AclProbe, type AclStatus, type Bootstrap,
} from './api';
import type { Notify } from './App';

interface Props {
  boot: Bootstrap;
  refresh: () => Promise<void>;
  notify: Notify;
}

const VIA_TEXT: Record<string, string> = {
  off: '账号权限未启用',
  header: '请求头识别',
  device: '本机设备绑定',
  session: '官方登录账号（自动识别）',
  unknown: '未识别',
};

const REASON_TEXT: Record<string, string> = {
  no_credential: '请求里没有登录凭证',
  no_mapping: '凭证未匹配到官方账号',
  no_session_table: '未在官方库发现会话表',
  'no-context': '内部调用缺少身份上下文',
};

function meText(me: AclStatus['me']): string {
  if (me.via === 'off') return '账号权限未启用（开启后按官方登录账号自动识别）';
  if (me.known) return `${me.name || me.uid}（${VIA_TEXT[me.via] || me.via}）`;
  const reason = REASON_TEXT[me.reason] || me.reason || '未知原因';
  return `未识别：${reason}`;
}

export function AccountsPage({ boot, refresh, notify }: Props) {
  const [status, setStatus] = useState<AclStatus | null>(null);
  const [active, setActive] = useState('');
  const [libs, setLibs] = useState<string[]>([]);
  const [sources, setSources] = useState<string[]>([]);
  const [busy, setBusy] = useState('');
  const [probe, setProbe] = useState<AclProbe | null>(null);
  const [bindUid, setBindUid] = useState('');
  const [newId, setNewId] = useState('');
  const [newName, setNewName] = useState('');
  const [showAdd, setShowAdd] = useState(false);

  const apply = useCallback((next: AclStatus, keep?: string) => {
    setStatus(next);
    setActive((prev) => {
      const current = keep ?? prev;
      if (next.subjects.some((s) => s.id === current)) return current;
      return next.subjects[0]?.id || '';
    });
  }, []);

  // 选中账号或授权数据变化时，把编辑区同步到已保存状态。
  useEffect(() => {
    const grant = status?.grants[active] || { libs: [], sources: [] };
    setLibs([...(grant.libs || [])]);
    setSources([...(grant.sources || [])]);
  }, [active, status]);

  const load = useCallback(async () => {
    try {
      apply(await api.aclStatus());
    } catch (err) {
      notify(err instanceof Error ? err.message : '读取账号权限失败', 'err');
    }
  }, [apply, notify]);

  useEffect(() => { void load(); }, [load]);

  async function run<T>(key: string, task: () => Promise<T>, done: (value: T) => void) {
    setBusy(key);
    try {
      done(await task());
    } catch (err) {
      notify(err instanceof Error ? err.message : '操作失败', 'err');
    } finally {
      setBusy('');
    }
  }

  const toggleEnabled = (value: boolean) => run(
    'enabled',
    () => api.aclConfig({ acl_enabled: value }),
    (next) => {
      apply(next);
      notify(value ? '账号权限已开启：未授权的账号看不到在线内容' : '账号权限已关闭', 'ok');
      void refresh();
    },
  );

  const saveGrant = () => {
    if (!active) return;
    const subject = status?.subjects.find((s) => s.id === active);
    return run('grant', () => api.aclGrant(active, libs, sources, subject?.name),
      (next) => {
        apply(next, active);
        notify('授权已保存并即时生效', 'ok');
      });
  };

  const revoke = () => {
    if (!active) return;
    return run('revoke', () => api.aclRevoke(active), (next) => {
      apply(next, active);
      notify('已清空该账号的全部授权', 'ok');
    });
  };

  const bind = () => {
    if (!bindUid) return;
    return run('bind', () => api.aclBind(bindUid), (result) => {
      apply(result.status, active);
      notify(`当前设备已绑定到 ${bindUid}`, 'ok');
    });
  };

  const unbind = () => run('bind', () => api.aclBind(''), (result) => {
    apply(result.status, active);
    notify('当前设备绑定已解除', 'ok');
  });

  const addSubject = () => {
    const id = newId.trim();
    if (!id) return;
    return run('add', () => api.aclAddSubject(id, newName.trim() || id), (next) => {
      apply(next, id);
      setNewId('');
      setNewName('');
      setShowAdd(false);
      notify('账号已添加，请勾选允许的内容', 'ok');
    });
  };

  const removeSubject = (uid: string) => run('del', () => api.aclDelSubject(uid), (next) => {
    apply(next);
    notify('已从清单移除（授权记录保留，重新添加即可恢复）', 'ok');
  });

  const discover = () => run('discover', async () => {
    const [next, info] = await Promise.all([api.aclStatus(), api.aclProbe()]);
    return { next, info };
  }, ({ next, info }) => {
    apply(next, active);
    setProbe(info);
    notify(`官方库发现 ${info.discover.users.length} 个账号`, 'ok');
  });

  const enabled = !!status?.enabled;
  const subject = status?.subjects.find((s) => s.id === active);
  const grant = status?.grants[active] || { libs: [], sources: [] };
  const boundHere = status?.me.fp
    ? status.devices.some((d) => d.fp === status.me.fp)
    : false;

  return (
    <>
      <div className="page-head">
        <h1>账号权限</h1>
        <p className="page-desc">
          按飞牛影视账号控制「能看到哪些虚拟媒体库」和「能搜索/播放哪些片源」。
          开启后未授权的账号看不到对应内容；官方本地媒体库不受影响。
        </p>
      </div>

      {status && !enabled && (
        <section className="card">
          <div className="row">
            <span className="badge">未启用</span>
            <span className="small">
              当前所有账号看到相同的在线内容。开启前请先在下方给账号勾选授权，
              否则开启瞬间所有账号（含管理员）的在线内容都会消失。
            </span>
          </div>
        </section>
      )}

      {status && enabled && !status.me.known && (
        <section className="card">
          <div className="row">
            <span className="badge danger">当前设备未识别</span>
            <span className="small">
              {REASON_TEXT[status.me.reason] || '当前设备没有被识别'}。
              在线内容已隐藏。请在下方「当前设备绑定」里把本设备绑定到一个账号，
              或先关闭账号权限。
            </span>
          </div>
        </section>
      )}

      <section className="card">
        <div className="card-title">总开关</div>
        <p className="card-sub">
          身份直接用飞牛影视的登录账号自动识别（读官方库的 token→账号映射），
          不需要手工绑定；设备绑定只在前一步识别不出来时兜底。管理页自身永不
          受权限限制，出问题可随时在这里关闭。
        </p>
        <label className="row" style={{ cursor: 'pointer' }}>
          <input
            className="check" type="checkbox" checked={enabled}
            disabled={busy === 'enabled'}
            onChange={(e) => void toggleEnabled(e.target.checked)}
          />
          <span>启用账号权限（未授权即不可见）</span>
        </label>
        <div className="row mt">
          <span className="small">当前身份：{status ? meText(status.me) : '读取中…'}</span>
          <button className="btn sm ghost" disabled={busy === 'discover'}
                  onClick={() => void discover()}>
            重新自检
          </button>
        </div>
        {probe && (
          <div className="stack mt">
            <div className="small">
              凭证候选 {probe.credential_candidates} 个 ·
              识别方式 {VIA_TEXT[probe.me.via] || probe.me.via}
              {probe.me.reason ? ` · ${REASON_TEXT[probe.me.reason] || probe.me.reason}` : ''}
            </div>
            <div className="small">
              Cookie 键：{probe.cookie_keys.length ? probe.cookie_keys.join('、') : '（无）'}
              {' · '}
              用户头：{Object.entries(probe.headers).filter(([, v]) => v).map(([k]) => k).join('、') || '（无）'}
            </div>
            <div className="small">
              官方库：{probe.discover.db || '未找到'}
              {probe.discover.tables.length
                ? ` · ${probe.discover.tables.map((t) => `${t.db}(${t.user_table || '无用户表'}${t.session_tables.length ? '/' + t.session_tables.join(',') : ''})`).join('；')}`
                : ' · 未发现用户表/会话表'}
              {probe.discover.error ? ` · 错误 ${probe.discover.error}` : ''}
            </div>
            <div className="small">
              会话映射：
              {probe.discover.sessions.length
                ? probe.discover.sessions.map(
                  (s) => `${s.db}.${s.table}(${s.token}→${s.uid})`).join('；')
                : '未发现（无法自动识别账号）'}
            </div>
            {probe.discover.users.length > 0 && (
              <div className="small">
                发现的账号：{probe.discover.users.map((u) => `${u.name}(${u.id})`).join('、')}
              </div>
            )}
          </div>
        )}
      </section>

      <section className="card">
        <div className="card-title">
          账号授权
          {status && <span className="small" style={{ marginLeft: 8, fontWeight: 400 }}>
            共 {status.subjects.length} 个账号
          </span>}
        </div>
        <p className="card-sub">
          账号由飞牛影视媒体库自动读取（含管理员标记），无需手工添加；
          勾选某个账号可看的虚拟库、可用的片源。未勾选 = 看不到、搜不到、
          播不了；片源还要求全局处于启用状态。
        </p>
        {status && (status.unassigned_subjects > 0 || status.unassigned_sources > 0) && (
          <div className="pill-row mb">
            {status.unassigned_subjects > 0 && (
              <span className="badge warn">
                {status.unassigned_subjects} 个账号尚无授权
              </span>
            )}
            {status.unassigned_sources > 0 && (
              <span className="badge warn">
                {status.unassigned_sources} / {status.enabled_sources} 个已启用源未授权给任何账号
              </span>
            )}
          </div>
        )}

        <div className="grid" style={{ gridTemplateColumns: 'minmax(220px, 0.8fr) minmax(320px, 2fr)' }}>
          <div className="stack">
            {!status?.subjects.length && (
              <div className="empty">
                <div className="empty-icon">◉</div>
                <div>还没有读到官方账号</div>
                <div className="small mt">
                  确认已安装飞牛影视并至少登录过一次，然后点上方「重新自检」。
                  仍读不到时可用下方「手动添加」兜底。
                </div>
              </div>
            )}
            {status?.subjects.map((item) => {
              const g = status.grants[item.id];
              const count = (g?.libs?.length || 0) + (g?.sources?.length || 0);
              return (
                <div key={item.id} className="row" style={{ gap: 6 }}>
                  <button
                    className={`btn sm ${item.id === active ? 'primary' : ''}`}
                    style={{ flex: 1, display: 'flex', justifyContent: 'space-between',
                             gap: 8, textAlign: 'left' }}
                    onClick={() => setActive(item.id)}
                  >
                    <span>
                      {item.name}
                      {item.is_admin && (
                        <span style={{ marginLeft: 6, fontSize: 11, opacity: 0.8 }}>
                          管理员
                        </span>
                      )}
                      {item.source === 'manual' && (
                        <span style={{ marginLeft: 6, fontSize: 11, opacity: 0.8 }}>
                          手动
                        </span>
                      )}
                    </span>
                    <span style={{ fontSize: 12, opacity: 0.95, fontWeight: 400 }}>
                      {count ? `${g?.libs?.length || 0}库/${g?.sources?.length || 0}源` : '未授权'}
                    </span>
                  </button>
                  {item.source === 'manual' && (
                    <button className="btn sm ghost" disabled={busy === 'del'}
                            onClick={() => void removeSubject(item.id)}>×</button>
                  )}
                </div>
              );
            })}
            <div className="divider" />
            {!showAdd ? (
              <button className="btn sm ghost" onClick={() => setShowAdd(true)}>
                + 手动添加（官方库读不到时兜底）
              </button>
            ) : (
              <div className="stack">
                <div>
                  <label className="field">账号标识（官方库里的 user guid）</label>
                  <input value={newId} onChange={(e) => setNewId(e.target.value)}
                         placeholder="从自检结果里的「发现的账号」复制" />
                </div>
                <div>
                  <label className="field">展示名（可选）</label>
                  <input value={newName} onChange={(e) => setNewName(e.target.value)}
                         placeholder="例如 客厅" />
                </div>
                <div className="row end">
                  <button className="btn sm ghost" onClick={() => setShowAdd(false)}>取消</button>
                  <button className="btn sm primary" disabled={!newId.trim() || busy === 'add'}
                          onClick={() => void addSubject()}>添加</button>
                </div>
              </div>
            )}
          </div>

          <div className="stack">
            {!subject ? (
              <div className="empty"><div>选择左侧账号后配置权限</div></div>
            ) : (
              <>
                <div className="row">
                  <strong>{subject.name}</strong>
                  <span className="small">id: {subject.id}</span>
                  <span className="small">
                    {grant.libs.length || grant.sources.length
                      ? `当前已授权 ${grant.libs.length} 库 / ${grant.sources.length} 源`
                      : '当前没有任何授权'}
                  </span>
                </div>

                <div className="divider" />
                <div className="row">
                  <label className="field" style={{ margin: 0 }}>当前设备绑定</label>
                  <select value={bindUid} style={{ maxWidth: 240 }}
                          onChange={(e) => setBindUid(e.target.value)}>
                    <option value="">选择账号…</option>
                    {status?.subjects.map((s) => (
                      <option key={s.id} value={s.id}>{s.name}</option>
                    ))}
                  </select>
                  <button className="btn sm info" disabled={!bindUid || busy === 'bind'}
                          onClick={() => void bind()}>
                    绑定本设备
                  </button>
                  {boundHere && (
                    <button className="btn sm ghost" disabled={busy === 'bind'}
                            onClick={() => void unbind()}>解除本机绑定</button>
                  )}
                </div>
                <div className="small">
                  仅在自动识别失败时用（例如播放器进程不带登录 token）。绑定按
                  凭证指纹记录，只存哈希；正常情况下不需要绑定。
                </div>

                <div className="divider" />
                <div className="row">
                  <label className="field" style={{ margin: 0 }}>虚拟媒体库</label>
                  <button className="btn sm ghost" onClick={() => setLibs(
                    libs.length === (status?.libs.length || 0) ? []
                      : (status?.libs.map((l) => l.key) || []))}>
                    {libs.length === (status?.libs.length || 0) ? '全不选' : '全选'}
                  </button>
                </div>
                <div className="grid">
                  {status?.libs.map((lib) => (
                    <label key={lib.key} className="row" style={{ cursor: 'pointer', gap: 6 }}>
                      <input
                        className="check" type="checkbox"
                        checked={libs.includes(lib.key)}
                        onChange={(e) => setLibs((cur) => e.target.checked
                          ? [...new Set([...cur, lib.key])]
                          : cur.filter((k) => k !== lib.key))}
                      />
                      <span>{lib.title}</span>
                      <span className="small">{lib.key}</span>
                    </label>
                  ))}
                </div>

                <div className="divider" />
                <div className="row">
                  <label className="field" style={{ margin: 0 }}>片源</label>
                  <button className="btn sm ghost" onClick={() => setSources(
                    sources.length === (status?.sources.length || 0) ? []
                      : (status?.sources.map((s) => s.id) || []))}>
                    {sources.length === (status?.sources.length || 0) ? '全不选' : '全选'}
                  </button>
                  <span className="small">未勾选的源：搜索结果不出现，也不能播放</span>
                </div>
                <div className="stack">
                  {status?.sources.map((src) => (
                    <label key={src.id} className="row" style={{ cursor: 'pointer', gap: 6 }}>
                      <input
                        className="check" type="checkbox"
                        checked={sources.includes(src.id)}
                        onChange={(e) => setSources((cur) => e.target.checked
                          ? [...new Set([...cur, src.id])]
                          : cur.filter((id) => id !== src.id))}
                      />
                      <span>{src.name || src.id}</span>
                      <span className="small">{src.id}</span>
                      {!src.enabled && <span className="badge warn">全局未启用</span>}
                    </label>
                  ))}
                </div>

                <div className="row end mt">
                  <button className="btn sm danger" disabled={busy === 'revoke'}
                          onClick={() => void revoke()}>清空授权</button>
                  <button className="btn primary" disabled={busy === 'grant'}
                          onClick={() => void saveGrant()}>
                    {busy === 'grant' ? '保存中…' : '保存授权'}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </section>

      <section className="card">
        <div className="card-title">当前已绑定的设备</div>
        <p className="card-sub">
          设备指纹只保存哈希，不含任何凭证明文。清理浏览器缓存或重新登录后指纹可能
          变化，需要重新绑定。
        </p>
        {!status?.devices.length ? (
          <div className="small">暂无设备绑定</div>
        ) : (
          <div className="stack">
            {status.devices.map((d) => (
              <div key={d.fp} className="row">
                <span className="badge">{status.grants[d.uid] ? '已授权账号' : '账号无授权'}</span>
                <code>{d.fp.slice(0, 12)}</code>
                <span>{d.uid}</span>
                <span className="small" style={{
                  maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}>{d.ua || '未知客户端'}</span>
                <span className="small">
                  {d.ts ? new Date(d.ts * 1000).toLocaleString() : ''}
                </span>
              </div>
            ))}
          </div>
        )}
        <div className="divider" />
        <div className="small">
          提示：账号权限是「家庭内软管控」，能防止家人误入未授权内容，
          不防御能直接改写请求头或拿到播放地址的技术用户。
        </div>
      </section>
    </>
  );
}
