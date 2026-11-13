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
  const [online, setOnline] = useState(true);
  const [busy, setBusy] = useState('');
  const [probe, setProbe] = useState<AclProbe | null>(null);
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

  // 选中账号或授权数据变化时，把编辑区同步到「生效值」：未单独配置的账号
  // 显示默认策略的结果，管理员看到的就是这个账号当前实际能看到的内容。
  useEffect(() => {
    const grant = status?.grants[active];
    const d = status?.defaults || { visible: true, online: true };
    setLibs(grant?.libs ?? (d.visible ? (status?.libs || []).map((l) => l.key) : []));
    setSources(grant?.sources ?? (d.online ? (status?.sources || []).map((s) => s.id) : []));
    setOnline(grant?.online ?? d.online);
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
    async () => {
      const next = await api.aclConfig({ enabled: value });
      if (next.enabled !== value) return next;
      // 身份是在请求开始时解析的：开关刚打开时，同一次请求里的 me 还是
      // 「未启用」，这里再取一次状态，页面才不会显示成没生效的样子。
      try {
        return await api.aclStatus();
      } catch {
        return next;
      }
    },
    (next) => {
      apply(next);
      // 以后端返回的状态为准：字段名对不上时接口也回 200，不能只看请求参数。
      if (next.enabled === value) {
        notify(value ? '账号权限已开启：未授权的账号看不到在线内容' : '账号权限已关闭', 'ok');
      } else {
        notify('开关没有生效，请刷新页面后重试', 'err');
      }
      void refresh();
    },
  );

  const saveGrant = () => {
    if (!active) return;
    const subject = status?.subjects.find((s) => s.id === active);
    return run('grant', () => api.aclGrant(active, libs, sources, subject?.name, online),
      (next) => {
        apply(next, active);
        notify('授权已保存并即时生效', 'ok');
      });
  };

  const revoke = () => {
    if (!active) return;
    return run('revoke', () => api.aclRevoke(active), (next) => {
      apply(next, active);
      notify('已恢复默认策略（不再单独限制该账号）', 'ok');
    });
  };

  const saveDefaults = (key: 'visible' | 'online', value: boolean) => run(
    'defaults',
    () => api.aclConfig(key === 'visible'
      ? { acl_default_visible: value }
      : { acl_default_online: value }),
    (next) => {
      apply(next, active);
      notify('默认策略已保存', 'ok');
    },
  );

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
    const still = next.subjects.some((s) => s.id === uid);
    notify(still
      ? '这是官方媒体库里的账号，不能删除；如需限制请取消它的全部授权'
      : '已删除该账号，并清掉了它的授权',
    still ? 'err' : 'ok');
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
  const grant = status?.grants[active];
  const configured = !!grant;

  return (
    <>
      <div className="page-head">
        <h1>账号权限</h1>
        <p className="page-desc">
          按飞牛影视账号控制「能看到哪些虚拟媒体库」和「能不能用在线搜索/播放」。
          账号自动读取、身份自动识别；没单独配置的账号按下方「默认策略」处理。
          官方本地媒体库不受影响。
        </p>
      </div>

      {status && !enabled && (
        <section className="card">
          <div className="row">
            <span className="badge">未启用</span>
            <span className="small">
              当前所有账号看到相同的在线内容。默认策略是「放行」，所以随时可以
              打开总开关，不会让任何人瞬间看不到东西；要限制谁，再去单独取消勾选。
            </span>
          </div>
        </section>
      )}

      {status && enabled && !status.me.known && status.subjects.length === 0 && (
        <section className="card">
          <div className="row">
            <span className="badge danger">没有读到官方账号</span>
            <span className="small">
              {REASON_TEXT[status.me.reason] || '官方库不可读'}。
              请确认已安装飞牛影视并登录过，然后点「重新自检」。此时各客户端都会
              被当作未授权，看不到在线内容。
            </span>
          </div>
        </section>
      )}

      <section className="card">
        <div className="card-title">总开关</div>
        <p className="card-sub">
          身份直接用飞牛影视的登录账号自动识别（读官方库的 token→账号映射），
          不需要做任何绑定。管理页自身永不受权限限制，出问题可随时在这里关闭。
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
        {status && enabled && !status.me.known && (
          <div className="small mt">
            这只是「当前浏览器」的身份 —— 管理页本身不带影视登录凭证，属正常。
            飞牛影视 App / TV 端按各自的登录凭证自动识别，不受这里影响。
          </div>
        )}
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
        <div className="card-title">默认策略</div>
        <p className="card-sub">
          没有单独配置的账号（包括暂时没识别出身份的客户端）按这里处理。
          默认全放行，所以开总开关是安全的；想默认收紧就取消勾选，再给需要的
          账号单独放开。
        </p>
        <label className="row" style={{ cursor: 'pointer' }}>
          <input
            className="check" type="checkbox"
            checked={!!status?.defaults.visible}
            disabled={busy === 'defaults'}
            onChange={(e) => void saveDefaults('visible', e.target.checked)}
          />
          <span>默认可看虚拟媒体库</span>
        </label>
        <label className="row" style={{ cursor: 'pointer' }}>
          <input
            className="check" type="checkbox"
            checked={!!status?.defaults.online}
            disabled={busy === 'defaults'}
            onChange={(e) => void saveDefaults('online', e.target.checked)}
          />
          <span>默认可在线搜索与播放</span>
        </label>
      </section>

      <section className="card">
        <div className="card-title">
          账号授权
          {status && <span className="small" style={{ marginLeft: 8, fontWeight: 400 }}>
            共 {status.subjects.length} 个账号
          </span>}
        </div>
        <p className="card-sub">
          账号由飞牛影视媒体库自动读取（含管理员标记），无需手工添加。给某个账号
          勾选可看的虚拟库、可用的片源并保存，就是给它单独配置；点「恢复默认策略」
          回到上面的默认处理。官方本地媒体库始终不受限制。
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

        <div className="grid acl-split">
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
              const summary = !g ? '默认'
                : g.online === false ? '在线禁用'
                  : `${g.libs?.length ?? '默认'}库`;
              return (
                <div key={item.id} className="row acl-account">
                  <button
                    className={`btn sm acl-account-btn ${item.id === active ? 'primary' : ''}`}
                    onClick={() => setActive(item.id)}
                  >
                    <span className="acl-account-name">
                      {item.name}
                      {item.is_admin && (
                        <span className="acl-tag">管理员</span>
                      )}
                      {item.source !== 'manual' && (
                        <span className="acl-tag">官方</span>
                      )}
                      {item.source === 'manual' && (
                        <span className="acl-tag">手动</span>
                      )}
                    </span>
                    <span className="acl-account-sum">{summary}</span>
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

          <div className="stack acl-panel">
            {!subject ? (
              <div className="empty"><div>选择账号后配置权限</div></div>
            ) : (
              <>
                <div className="row">
                  <strong>{subject.name}</strong>
                  <span className="small acl-id">id: {subject.id}</span>
                  <span className="small">
                    {configured
                      ? `已单独配置：${libs.length} 库 / ${online ? `${sources.length} 源` : '在线已禁用'}`
                      : '未单独配置，按默认策略'}
                  </span>
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
                <label className="row" style={{ cursor: 'pointer' }}>
                  <input
                    className="check" type="checkbox" checked={online}
                    onChange={(e) => setOnline(e.target.checked)}
                  />
                  <span>允许该账号在线搜索与播放</span>
                  <span className="small">
                    关掉后该账号只能用官方本地媒体库，看不到也搜不到在线内容
                  </span>
                </label>

                <div className="divider" />
                <div className="row">
                  <label className="field" style={{ margin: 0 }}>片源</label>
                  <button className="btn sm ghost" onClick={() => setSources(
                    sources.length === (status?.sources.length || 0) ? []
                      : (status?.sources.map((s) => s.id) || []))}>
                    {sources.length === (status?.sources.length || 0) ? '全不选' : '全选'}
                  </button>
                  <span className="small">
                    {online
                      ? '未勾选的源：搜索结果不出现，也不能播放'
                      : '在线已禁用，下面的源暂不生效'}
                  </span>
                </div>
                <div className="stack">
                  {status?.sources.map((src) => (
                    <label key={src.id} className="row" style={{ cursor: 'pointer', gap: 6 }}>
                      <input
                        className="check" type="checkbox"
                        checked={sources.includes(src.id)}
                        disabled={!online}
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
                          onClick={() => void revoke()}>恢复默认策略</button>
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
        <div className="card-title">说明</div>
        <p className="card-sub">
          身份来自飞牛影视自己的登录凭证：客户端登录哪个账号，就按哪个账号的授权
          与播放记录处理，不需要在这里做任何绑定。
        </p>
        <div className="small">
          账号权限是「家庭内软管控」，能防止家人误入未授权内容，不防御能直接改写
          请求头或拿到播放地址的技术用户。
        </div>
      </section>
    </>
  );
}
