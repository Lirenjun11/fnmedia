import { useCallback, useEffect, useRef, useState } from 'react';
import { api, type AdPreview, type Bootstrap, type Config, type UpdateInfo } from './api';
import type { Notify } from './App';

interface Props {
  boot: Bootstrap;
  refresh: () => Promise<void>;
  notify: Notify;
}

export function PlaybackPage({ boot, refresh, notify }: Props) {
  const [form, setForm] = useState<Config>({ ...boot.config });
  const [saving, setSaving] = useState(false);

  const set = (key: keyof Config, value: number | boolean) =>
    setForm((current) => ({ ...current, [key]: value }));

  async function save() {
    setSaving(true);
    try {
      await api.patchConfig({
        enabled_limit: Number(form.enabled_limit),
        search_timeout: Number(form.search_timeout),
        search_concurrency: Number(form.search_concurrency),
        play_timeout: Number(form.play_timeout),
        online_first: Boolean(form.online_first),
        merge_sources: Boolean(form.merge_sources),
        auto_best_source: Boolean(form.auto_best_source),
      });
      await refresh();
      notify('设置已保存并即时生效', 'ok');
    } catch (err) {
      notify(err instanceof Error ? err.message : '保存失败', 'err');
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>搜索与播放</h1>
        <p className="page-desc">固定预算策略：源数量增加不会延长等待时间</p>
      </div>
      <section className="card">
        <div className="card-title">搜索</div>
        <p className="card-sub">所有启用的源并行搜索，到点即返回已有结果并取消慢源。</p>
        <div className="grid">
          <div>
            <label className="field">总预算（秒，0.5 – 10）</label>
            <input type="number" step="0.1" min="0.5" max="10" value={form.search_timeout}
                   onChange={(e) => set('search_timeout', Number(e.target.value))} />
          </div>
          <div>
            <label className="field">并发上限（1 – 16）</label>
            <input type="number" min="1" max="16" value={form.search_concurrency}
                   onChange={(e) => set('search_concurrency', Number(e.target.value))} />
          </div>
          <div>
            <label className="field">参与搜索源上限（1 – 200）</label>
            <input type="number" min="1" max="200" value={form.enabled_limit}
                   onChange={(e) => set('enabled_limit', Number(e.target.value))} />
          </div>
          <div>
            <label className="field">播放清单超时（秒）</label>
            <input type="number" step="0.5" min="2" max="30" value={form.play_timeout}
                   onChange={(e) => set('play_timeout', Number(e.target.value))} />
          </div>
        </div>
        <div className="divider" />
        <label className="row" style={{ cursor: 'pointer' }}>
          <input
            className="check" type="checkbox" checked={!!form.merge_sources}
            onChange={(e) => set('merge_sources', e.target.checked)}
          />
          <span>
            跨源聚合搜索：同名影片合并为一张卡，换线菜单显示“源名称：线路名”
          </span>
        </label>
        <label className="row mt" style={{ cursor: 'pointer' }}>
          <input
            className="check" type="checkbox" checked={!!form.online_first}
            onChange={(e) => set('online_first', e.target.checked)}
          />
          <span>在线结果排在搜索结果前面</span>
        </label>
        <label className="row mt" style={{ cursor: 'pointer' }}>
          <input
            className="check" type="checkbox" checked={!!form.auto_best_source}
            onChange={(e) => set('auto_best_source', e.target.checked)}
          />
          <span>起播时自动优选最快线路（并发探测 1.5 秒预算）</span>
        </label>
        <div className="row end mt">
          <button className="btn primary" disabled={saving} onClick={() => void save()}>
            {saving ? '保存中…' : '保存设置'}
          </button>
        </div>
      </section>
      <section className="card">
        <div className="card-title">不做的事</div>
        <div className="pill-row mt">
          <span className="pill">无全站测速</span>
          <span className="pill">无常驻预热</span>
          <span className="pill">无占位轮询</span>
          <span className="pill">无后台自愈</span>
          <span className="pill">无健康画像</span>
          <span className="pill">无磁盘缓存</span>
        </div>
      </section>
    </>
  );
}

export function AdFilterPage({ boot, refresh, notify }: Props) {
  const [form, setForm] = useState<Config>({ ...boot.config });
  const [keywords, setKeywords] = useState(
    (boot.config.adfilter_keywords || []).join('\n'),
  );
  const [saving, setSaving] = useState(false);
  const [previews, setPreviews] = useState<AdPreview[]>([]);
  const [loadingPreview, setLoadingPreview] = useState(false);

  const set = (key: keyof Config, value: number | boolean) =>
    setForm((current) => ({ ...current, [key]: value }));

  const loadPreview = useCallback(async () => {
    setLoadingPreview(true);
    try {
      const data = await api.adfilterPreview();
      setPreviews(data.previews);
    } catch (err) {
      notify(err instanceof Error ? err.message : '读取诊断失败', 'err');
    } finally {
      setLoadingPreview(false);
    }
  }, [notify]);

  useEffect(() => { void loadPreview(); }, [loadPreview]);

  async function save() {
    setSaving(true);
    try {
      await api.patchConfig({
        adfilter_enabled: Boolean(form.adfilter_enabled),
        adfilter_markers: Boolean(form.adfilter_markers),
        adfilter_head_skip: Number(form.adfilter_head_skip),
        adfilter_head_guard: Boolean(form.adfilter_head_guard),
        adfilter_breaks: Boolean(form.adfilter_breaks),
        adfilter_keywords: keywords
          .split(/[\n,]/)
          .map((word) => word.trim())
          .filter(Boolean)
          .slice(0, 32),
      });
      await refresh();
      notify('广告过滤设置已保存并即时生效', 'ok');
    } catch (err) {
      notify(err instanceof Error ? err.message : '保存失败', 'err');
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>广告切片过滤</h1>
        <p className="page-desc">按播放清单里的文本特征跳过广告分片，默认关闭</p>
      </div>
      <section className="card">
        <div className="card-title">总开关</div>
        <label className="row" style={{ cursor: 'pointer' }}>
          <input
            className="check" type="checkbox" checked={!!form.adfilter_enabled}
            onChange={(e) => set('adfilter_enabled', e.target.checked)}
          />
          <span>启用广告分片过滤</span>
        </label>
        <p className="card-sub mt">
          只读取 m3u8 文本（时长、分片地址、广告标记），不下载分片内容、不增加起播等待。
          规则改动即时生效，无需清理缓存。误判时关掉本开关即可原样恢复。
        </p>
        <div className="divider" />
        <label className="row" style={{ cursor: 'pointer' }}>
          <input
            className="check" type="checkbox" checked={!!form.adfilter_markers}
            onChange={(e) => set('adfilter_markers', e.target.checked)}
          />
          <span>识别显式广告标记（CUE-OUT / CUE-IN、广告 DATERANGE）</span>
        </label>
        <label className="row mt" style={{ cursor: 'pointer' }}>
          <input
            className="check" type="checkbox" checked={!!form.adfilter_head_guard}
            onChange={(e) => set('adfilter_head_guard', e.target.checked)}
          />
          <span>片头跳过时保护疑似正片分片（推荐开启）</span>
        </label>
        <label className="row mt" style={{ cursor: 'pointer' }}>
          <input
            className="check" type="checkbox" checked={!!form.adfilter_breaks}
            onChange={(e) => set('adfilter_breaks', e.target.checked)}
          />
          <span>识别 DISCONTINUITY 分隔的短广告段（主流播放器做法）</span>
        </label>
        <p className="hint mt">
          分段识别按 DISCONTINUITY 把清单切组，仅当某组 3–15 片、3–120 秒、
          且不足最长组 5% 时才判为广告；单组或分组过密的清单不处理。
          另含重复分片、极短追踪分片、长清单中的少数派域名三类强证据。
          一次删除超过 20%（分段规则）或半数时长、或会删空清单时，整次过滤自动回退。
        </p>
        <div className="grid mt">
          <div>
            <label className="field">片头跳过（秒，0 关闭，最多 120）</label>
            <input type="number" min="0" max="120" step="1"
                   value={form.adfilter_head_skip}
                   onChange={(e) => set('adfilter_head_skip', Number(e.target.value))} />
          </div>
        </div>
        <div className="mt">
          <label className="field">分片地址关键字（每行一个，最多 32 个，不区分大小写）</label>
          <textarea rows={4} value={keywords}
                    placeholder={'例如：/ad/\n例如：advert'}
                    onChange={(e) => setKeywords(e.target.value)} />
        </div>
        <p className="hint mt">
          关键字只匹配分片 URL；不同源用不同域名时可分别填写。建议先在「日志诊断」页观察
          adfilter 命中记录，再逐步收紧规则。不要用「分片时长短」当判据，正片常见 6 秒切片。
        </p>
        <div className="row end mt">
          <button className="btn primary" disabled={saving} onClick={() => void save()}>
            {saving ? '保存中…' : '保存设置'}
          </button>
        </div>
      </section>
      <section className="card">
        <div className="card-title">最近清单诊断</div>
        <p className="card-sub">
          进程内保留最近 5 次清单改写记录，不落盘。这里能看到每个被删分片的地址、时长与命中原因。
        </p>
        <div className="row mb">
          <button className="btn sm" disabled={loadingPreview}
                  onClick={() => void loadPreview()}>
            {loadingPreview ? '读取中…' : '刷新记录'}
          </button>
          <span className="small">仅在启用过滤后出现记录</span>
        </div>
        {previews.length === 0 ? (
          <p className="hint">暂无记录。启用过滤并播放一个订阅源条目后再刷新。</p>
        ) : (
          <div className="stack">
            {previews.slice().reverse().map((item, i) => (
              <div className="card" key={`${item.ts}-${i}`}>
                <div className="row">
                  <span className="small">
                    {new Date(item.ts * 1000).toLocaleTimeString()}
                  </span>
                  <span className="badge">
                    删除 {item.removed} 片 / {item.removed_duration}s
                  </span>
                  <span className="badge ok">
                    保留 {item.kept_duration}s / 共 {item.total_duration}s
                  </span>
                  {item.guard && <span className="badge warn">保护：{item.guard}</span>}
                </div>
                <div className="row mt">
                  <span className="small">
                    命中：标记 {item.by_marker} / 关键字 {item.by_keyword} / 片头 {item.by_head} /
                    分段 {item.by_break} / 重复 {item.by_duplicate} / 极短 {item.by_tiny} /
                    少数域名 {item.by_minority}
                  </span>
                </div>
                {(item.groups || []).length > 1 && (
                  <p className="hint">
                    分组：
                    {item.groups.map((g) => `#${g.group} ${g.count}片/${g.duration}s`).join('，')}
                  </p>
                )}
                {item.guard_kept > 0 && (
                  <p className="hint mt">
                    疑似正片分片 {item.guard_kept} 个已被保护，未删除。
                  </p>
                )}
                {item.dropped.length > 0 && (
                  <div className="log-box mt">
                    {item.dropped.map((seg, j) => (
                      <div key={j}>[{seg.reason}] {seg.duration}s {seg.url}</div>
                    ))}
                  </div>
                )}
                <details className="mt">
                  <summary className="small">查看清单改写前后片段</summary>
                  <div className="log-box mt">原始{'\n'}{item.raw_head}</div>
                  <div className="log-box mt">过滤后{'\n'}{item.kept_head}</div>
                </details>
              </div>
            ))}
          </div>
        )}
      </section>
      <section className="card">
        <div className="card-title">边界说明</div>
        <p className="small">
          仅作用于订阅源的 HLS 媒体清单，官方本地影片完全不受影响。加密清单会保留每个分片的
          原始序号，避免解密错位；片头跳过会同步修正展示时长。此功能可能改变源站既有的
          广告呈现，请自行确认使用合法性。
        </p>
      </section>
    </>
  );
}

export function CachePage({ boot, refresh, notify }: Props) {
  const [busy, setBusy] = useState(false);
  async function clear() {
    setBusy(true);
    try {
      const result = await api.clearCache();
      await refresh();
      notify(`已清理 ${result.cleared} 条搜索缓存`, 'ok');
    } catch (err) {
      notify(err instanceof Error ? err.message : '清理失败', 'err');
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="page-head">
        <h1>缓存与记录</h1>
        <p className="page-desc">只有内存缓存，重启即清空，不写磁盘</p>
      </div>
      <div className="grid mb">
        <div className="stat"><div className="value">{boot.titles_cached}</div><div className="label">内存标题条目</div></div>
        <div className="stat"><div className="value">2</div><div className="label">缓存层数</div></div>
        <div className="stat"><div className="value">1</div><div className="label">磁盘配置文件</div></div>
      </div>
      <section className="card">
        <div className="card-title">搜索结果缓存</div>
        <p className="card-sub">
          仅内存、TTL 120 秒、带单飞；播放清单只在单次请求内使用，不落盘。
          播放记录与续播沿用官方影视自身能力，本扩展不额外持久化。
        </p>
        <button className="btn" disabled={busy} onClick={() => void clear()}>
          {busy ? '清理中…' : '清理搜索缓存'}
        </button>
      </section>
    </>
  );
}

export function LogsPage({ notify }: { notify: Notify }) {
  const [lines, setLines] = useState<{ ts: number; msg: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [paused, setPaused] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);

  const load = useCallback(async (reportError = false) => {
    try {
      const data = await api.logs();
      setLines(data.logs);
      setLoaded(true);
    } catch (err) {
      if (reportError) {
        notify(err instanceof Error ? err.message : '读取失败', 'err');
      }
    }
  }, [notify]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => {
      if (!paused && document.visibilityState === 'visible') void load();
    }, 2000);
    return () => window.clearInterval(timer);
  }, [load, paused]);

  useEffect(() => {
    const box = boxRef.current;
    if (box && followRef.current) box.scrollTop = box.scrollHeight;
  }, [lines]);

  function onScroll() {
    const box = boxRef.current;
    if (box) {
      followRef.current = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    }
  }

  async function manual() {
    setBusy(true);
    await load(true);
    setBusy(false);
  }

  return (
    <>
      <div className="page-head">
        <h1>日志与诊断</h1>
        <p className="page-desc">打开页面自动加载并实时刷新，离开页面即停止</p>
      </div>
      <section className="card">
        <div className="row mb">
          <button className="btn primary sm" disabled={busy} onClick={() => void manual()}>
            立即刷新
          </button>
          <button className="btn sm" onClick={() => setPaused(!paused)}>
            {paused ? '继续自动刷新' : '暂停自动刷新'}
          </button>
          <span className="small">
            {paused ? '已暂停' : '每 2 秒刷新（页面隐藏时暂停）'}；
            进程内环形缓冲，最多 200 条，不落盘
          </span>
        </div>
        {!loaded ? (
          <p className="hint">正在加载日志…</p>
        ) : lines.length === 0 ? (
          <p className="hint">暂无日志记录。</p>
        ) : (
          <div className="log-box" ref={boxRef} onScroll={onScroll}>
            {lines.map((line, i) => (
              <div key={`${line.ts}-${i}`}>
                [{new Date(line.ts * 1000).toLocaleTimeString()}] {line.msg}
              </div>
            ))}
          </div>
        )}
      </section>
    </>
  );
}

export function AboutPage({ boot, update, checking, onCheck }: Props & {
  update: UpdateInfo | null;
  checking: boolean;
  onCheck: () => void;
}) {
  return (
    <>
      <div className="page-head">
        <h1>关于</h1>
        <p className="page-desc">版本信息与免责声明</p>
      </div>
      <section className="card">
        <div className="card-title">fnmedia</div>
        <div className="pill-row mt">
          <span className="badge brand">v{boot.version}</span>
          <span className="badge">零内置源</span>
          <span className="badge ok">可逆接管</span>
          {update?.has_update && <span className="badge danger">NEW</span>}
        </div>
        <p className="hint mt">
          零侵入、订阅驱动、带完整 Web 配置页的飞牛影视扩展。停用或卸载后，
          官方影视立即原样恢复。
        </p>
      </section>
      <section className="card">
        <div className="card-title">检查更新</div>
        <div className="row">
          <button className="btn sm primary" disabled={checking}
                  onClick={onCheck}>
            {checking ? '检查中…' : '检查更新'}
          </button>
          <span className="small">当前版本 v{boot.version}</span>
        </div>
        {update && (
          <div className="stack mt">
            {update.error ? (
              <span className="small">暂时无法获取更新信息</span>
            ) : (
              <>
                <div className="row">
                  {update.has_update ? (
                    <span className="badge warn">发现新版本 v{update.latest}</span>
                  ) : (
                    <span className="badge ok">
                      已是最新版本（v{update.latest || update.current}）
                    </span>
                  )}
                  {update.published_at && (
                    <span className="small">
                      发布于 {update.published_at.slice(0, 10)}
                    </span>
                  )}
                  <span className="small">
                    {update.cached ? '（缓存结果）' : '（刚刚检查）'}
                  </span>
                </div>
                {update.notes && (
                  <>
                    <span className="small">更新说明（最新正式版 v{update.latest}）</span>
                    <div className="log-box">{update.notes}</div>
                  </>
                )}
                {update.html_url && (
                  <div className="row">
                    <a className="btn primary sm" href={update.html_url}
                       target="_blank" rel="noreferrer">
                      {update.has_update ? '前往下载' : '查看发布页'}
                    </a>
                    {update.assets.map((asset) => asset.url && (
                      <a key={asset.name} className="btn sm" href={asset.url}
                         target="_blank" rel="noreferrer">
                        {asset.name}
                      </a>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </section>
      <section className="card">
        <div className="card-title">免责声明</div>
        <p className="small">
          本项目是开源免费的技术研究 / 个人媒体库工具，仅在你自己的 NAS 上运行。
          它本身不提供、不存储、不分发任何影视内容、图片、播放地址，也不内置任何资源站或订阅地址。
          本项目不提供、不推荐、不解答任何资源站点的名称、地址或获取方法；
          接口由使用者自行配置并自行核实合法性。仅供学习交流与个人合法播放自有或已获授权内容使用，
          请勿商用或传播侵权内容，由此产生的法律责任由使用者自行承担。
        </p>
      </section>
    </>
  );
}
