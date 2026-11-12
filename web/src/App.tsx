import { useCallback, useEffect, useState } from 'react';
import { api, type Bootstrap, type UpdateInfo } from './api';
import SourcesPage from './Sources';
import SubscriptionsPage from './Subscriptions';
import PreferencesPage from './Preferences';
import { AboutPage, CachePage, LogsPage, PlaybackPage } from './Settings';

export type Page =
  | 'overview' | 'sources' | 'subscriptions' | 'playback'
  | 'cache' | 'logs' | 'settings' | 'about';
export type Notify = (message: string, kind?: 'ok' | 'err' | '') => void;

const NAV: { key: Page; label: string; icon: string }[] = [
  { key: 'overview', label: '总览', icon: '◉' },
  { key: 'sources', label: '片源配置', icon: '▦' },
  { key: 'subscriptions', label: '订阅配置', icon: '◈' },
  { key: 'playback', label: '搜索播放', icon: '▷' },
  { key: 'cache', label: '缓存记录', icon: '▤' },
  { key: 'logs', label: '日志诊断', icon: '≡' },
  { key: 'settings', label: '设置', icon: '⚙' },
  { key: 'about', label: '关于', icon: 'ⓘ' },
];

export default function App() {
  const [page, setPage] = useState<Page>('overview');
  const [boot, setBoot] = useState<Bootstrap | null>(null);
  const [error, setError] = useState('');
  const [toast, setToast] = useState<{ message: string; kind: string } | null>(null);
  const [updateInfo, setUpdateInfo] = useState<UpdateInfo | null>(null);
  const [checkingUpdate, setCheckingUpdate] = useState(false);

  const checkUpdate = useCallback(async (force = false) => {
    setCheckingUpdate(true);
    try {
      setUpdateInfo(await api.checkUpdate(force));
    } catch {
      setUpdateInfo({
        current: '', latest: '', has_update: false, html_url: '',
        notes: '', assets: [], published_at: '', error: 'unavailable',
        cached: false,
      });
    } finally {
      setCheckingUpdate(false);
    }
  }, []);

  const notify: Notify = useCallback((message, kind = '') => {
    setToast({ message, kind });
    window.setTimeout(() => setToast(null), 3200);
  }, []);

  const refresh = useCallback(async (attempt = 0): Promise<void> => {
    try {
      const data = await api.bootstrap();
      setBoot(data);
      setError('');
    } catch (err) {
      if (attempt < 4) {
        window.setTimeout(() => void refresh(attempt + 1), 600 * (attempt + 1));
      }
      setError(err instanceof Error ? err.message : '服务尚未就绪');
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { void checkUpdate(); }, [checkUpdate]);

  const hasUpdate = !!updateInfo?.has_update;

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          <img className="brand-mark" src="/v/ext/app-icon.png" alt="" />
          <div>
            <div className="brand-name">fnmedia</div>
            <div className="brand-sub">飞牛影视扩展</div>
          </div>
        </div>
        <nav className="nav">
          <div className="nav-label">管理</div>
          {NAV.map((item) => (
            <button
              key={item.key}
              className={`nav-item ${page === item.key ? 'active' : ''}`}
              onClick={() => setPage(item.key)}
            >
              <span className="ico">{item.icon}</span>
              <span>{item.label}</span>
              {item.key === 'about' && hasUpdate && <span className="nav-dot" />}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div>v{boot?.version || '0.0.1.0'} · 零内置源</div>
        </div>
      </aside>

      <main className="main">
        {!boot && error ? (
          <div className="center-state">
            <div className="spinner" />
            <div>服务启动中，正在自动重试…</div>
            <div className="small mt">{error}</div>
            <button className="btn mt" onClick={() => void refresh()}>立即重试</button>
          </div>
        ) : (
          <>
            {page === 'overview' && (
              <Overview boot={boot} onGo={() => setPage('subscriptions')} />
            )}
            {page === 'sources' && boot && (
              <SourcesPage boot={boot} refresh={refresh} notify={notify} />
            )}
            {page === 'subscriptions' && boot && (
              <SubscriptionsPage boot={boot} refresh={refresh} notify={notify} />
            )}
            {page === 'playback' && boot && (
              <PlaybackPage boot={boot} refresh={refresh} notify={notify} />
            )}
            {page === 'cache' && boot && (
              <CachePage boot={boot} refresh={refresh} notify={notify} />
            )}
            {page === 'logs' && <LogsPage notify={notify} />}
            {page === 'settings' && boot && (
              <PreferencesPage boot={boot} refresh={refresh} notify={notify} />
            )}
            {page === 'about' && boot && (
              <AboutPage boot={boot} refresh={refresh} notify={notify}
                         update={updateInfo} checking={checkingUpdate}
                         onCheck={() => void checkUpdate(true)} />
            )}
          </>
        )}
      </main>

      {toast && <div className={`toast ${toast.kind}`}>{toast.message}</div>}
    </div>
  );
}

function Overview({ boot, onGo }: { boot: Bootstrap | null; onGo: () => void }) {
  if (boot && boot.sources_total === 0) {
    return (
      <>
        <div className="page-head">
          <h1>总览</h1>
          <p className="page-desc">尚未配置任何资源站，扩展当前处于纯透传状态</p>
        </div>
        <div className="empty">
          <div className="empty-icon">◈</div>
          <h2>先添加你的第一个源</h2>
          <p>
            本扩展不内置、不推荐、不分发任何资源站或订阅地址。请粘贴你自己持有的聚合源订阅，
            或手动填写一个苹果 CMS 采集接口。未配置时，官方影视的所有功能保持原样。
          </p>
          <button className="btn primary" onClick={onGo}>前往订阅配置</button>
        </div>
        <div className="card mt">
          <div className="card-title">当前状态</div>
          <div className="pill-row mt">
            <span className="badge ok">官方服务纯透传</span>
            <span className="badge">启用源 0</span>
            <span className="badge">零后台任务</span>
            <span className="badge">零外连</span>
          </div>
        </div>
      </>
    );
  }

  const stats = [
    { label: '已启用源', value: String(boot?.sources_enabled ?? 0), on: (boot?.sources_enabled ?? 0) > 0 },
    { label: '源总数', value: String(boot?.sources_total ?? 0), on: false },
    { label: '在线功能', value: boot?.online_enabled ? '已启用' : '未启用', on: !!boot?.online_enabled },
    { label: '内存标题', value: String(boot?.titles_cached ?? 0), on: false },
  ];
  return (
    <>
      <div className="page-head">
        <h1>总览</h1>
        <p className="page-desc">服务状态一览；本页不会触发任何探测或刷新</p>
      </div>
      <div className="grid mb">
        {stats.map((s) => (
          <div className="stat" key={s.label}>
            <div className={`value ${s.on ? 'on' : ''}`}>{s.value}</div>
            <div className="label">{s.label}</div>
          </div>
        ))}
      </div>
      <div className="card">
        <div className="card-title">运行信息</div>
        <div className="pill-row mt">
          <span className="badge brand">v{boot?.version || '0.0.1.0'}</span>
          <span className="badge">运行 {Math.round((boot?.uptime ?? 0) / 60)} 分钟</span>
          <span className="badge ok">上游接管正常</span>
        </div>
        <p className="hint">
          搜索使用固定并发与总预算，源数量增加不会改变超时；起播默认并发优选最快线路，
          清单失败自动换线；无常驻预热与后台自愈。
        </p>
      </div>
    </>
  );
}
