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
        search_debounce_ms: Number(form.search_debounce_ms),
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
        <p className="card-sub">
          所有启用的源并行搜索，到点即返回已有结果并取消慢源。
          输入防抖在扩展侧按会话合并连续搜索：录入过程中的中间词只返回官方本地结果，
          仅最后一次查询在线源，飞牛客户端的请求时机与官方结果不受影响。
        </p>
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
            <label className="field">输入防抖（毫秒，0 关闭，最多 1000）</label>
            <input type="number" min="0" max="1000" step="50"
                   value={form.search_debounce_ms}
                   onChange={(e) => set('search_debounce_ms', Number(e.target.value))} />
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
        adfilter_strong: Boolean(form.adfilter_strong),
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
        <label className="row mt" style={{ cursor: 'pointer' }}>
          <input
            className="check" type="checkbox" checked={!!form.adfilter_strong}
            onChange={(e) => set('adfilter_strong', e.target.checked)}
          />
          <span>强证据识别：重复分片 / 极短追踪分片 / 少数派域名</span>
        </label>
        <p className="hint mt">
          分段识别按 DISCONTINUITY 把清单切组，仅当某组 3–15 片、3–120 秒、
          且不足最长组 5% 时才判为广告；单组或分组过密的清单不处理。
          强证据为无条件自动推断，误判风险同源，故与分段识别分开独立开关；
          若某源会循环复用正片分片，单独关掉它即可。
          删除广告段后清理其出口处残留的 DISCONTINUITY 标签，正片内部合法切换保留。
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
                    少数域名 {item.by_minority} / 清理标记 {item.disc_cleaned}
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

const VLIB_CATS = [
  { key: 'movie', label: '电影' },
  { key: 'tv', label: '剧集' },
  { key: 'anime', label: '动漫' },
  { key: 'variety', label: '综艺' },
];

function timeText(ts: number): string {
  return ts ? new Date(ts * 1000).toLocaleString() : '尚未刷新';
}

export function VirtualLibPage({ boot, refresh, notify }: Props) {
  const [form, setForm] = useState({
    vlib_enabled: !!boot.config.vlib_enabled,
    vlib_charts: !!boot.config.vlib_charts,
    vlib_catalog: !!boot.config.vlib_catalog,
    vlib_refresh_hours: Number(boot.config.vlib_refresh_hours ?? 6),
    vlib_sources: [...(boot.config.vlib_sources || [])],
    vlib_categories: [...(boot.config.vlib_categories || [])],
    vlib_split: boot.config.vlib_split || 'source_category',
  });
  const [status, setStatus] = useState(boot.vlib);
  const [busy, setBusy] = useState('');

  const set = (key: keyof typeof form, value: number | boolean | string | string[]) =>
    setForm((current) => ({ ...current, [key]: value }));

  function toggleId(key: 'vlib_sources' | 'vlib_categories', id: string) {
    setForm((current) => {
      const cur = current[key];
      return { ...current, [key]: cur.includes(id)
        ? cur.filter((x) => x !== id) : [...cur, id] };
    });
  }

  async function save() {
    setBusy('save');
    try {
      await api.patchConfig({
        vlib_enabled: form.vlib_enabled,
        vlib_charts: form.vlib_charts,
        vlib_catalog: form.vlib_catalog,
        vlib_refresh_hours: Number(form.vlib_refresh_hours),
        vlib_sources: form.vlib_sources,
        vlib_categories: form.vlib_categories,
        vlib_split: form.vlib_split,
      });
      await refresh();
      setStatus(await api.vlibStatus());
      notify('虚拟媒体库设置已保存并即时生效', 'ok');
    } catch (err) {
      notify(err instanceof Error ? err.message : '保存失败', 'err');
    } finally {
      setBusy('');
    }
  }

  async function run(kind: 'refresh' | 'clear') {
    setBusy(kind);
    try {
      const data = kind === 'refresh'
        ? await api.vlibRefresh('all')
        : await api.vlibClear();
      setStatus(data);
      notify(kind === 'refresh' ? '已触发刷新，稍后重新进入影视即可看到' : '缓存已清空',
             'ok');
    } catch (err) {
      notify(err instanceof Error ? err.message : '操作失败', 'err');
    } finally {
      setBusy('');
    }
  }

  const chartCounts = status?.charts?.counts || {};
  const routes = status?.catalog?.routes || {};
  const bySource = status?.catalog?.by_source || {};

  return (
    <>
      <div className="page-head">
        <h1>虚拟媒体库</h1>
        <p className="page-desc">在飞牛影视媒体库首页注入在线榜单与海量分类库</p>
      </div>
      <section className="card">
        <div className="card-title">总开关</div>
        <label className="row" style={{ cursor: 'pointer' }}>
          <input className="check" type="checkbox" checked={form.vlib_enabled}
                 onChange={(e) => set('vlib_enabled', e.target.checked)} />
          <span>注入虚拟媒体库（关掉后媒体库首页立即恢复官方原样）</span>
        </label>
        <div className="divider" />
        <label className="row" style={{ cursor: 'pointer' }}>
          <input className="check" type="checkbox" checked={form.vlib_charts}
                 onChange={(e) => set('vlib_charts', e.target.checked)} />
          <span>榜单四库：🎬热门电影 / 📺热门剧集 / 🌸动漫新番 / 🎤热门综艺</span>
        </label>
        <p className="card-sub">
          展示层来自豆瓣公开榜单（片名、海报、评分），点进详情时才用片名聚合搜索
          你的资源站；命中后复用既有详情、选集、线路与续播能力，未命中显示「暂无在线片源」。
        </p>
        <label className="row" style={{ cursor: 'pointer' }}>
          <input className="check" type="checkbox" checked={form.vlib_catalog}
                 onChange={(e) => set('vlib_catalog', e.target.checked)} />
          <span>海量四库：🎞海量电影 / 📺海量剧集 / 🌸海量动漫 / 🎤海量综艺</span>
        </label>
        <p className="card-sub">
          从资源站 Apple CMS 分类表里按「最近更新」深翻，每页直连详情接口，
          点开即可播放，无需再搜索。脏类（解说/预告/短剧/福利等）自动剔除。
        </p>
        <div className="grid mt">
          <div>
            <label className="field">刷新间隔（小时，1 – 24）</label>
            <input type="number" min="1" max="24" value={form.vlib_refresh_hours}
                   onChange={(e) => set('vlib_refresh_hours', Number(e.target.value))} />
          </div>
        </div>

        <div className="divider" />
        <div className="card-title">片源映射</div>
        <p className="card-sub">
          不勾选任何片源 = 沿用全部启用源（旧的「榜单四库 + 海量四库」）。勾选后
          只有选中的源会进入虚拟库；账号只看到自己被授权的源对应的库。
        </p>
        <label className="field">映射到虚拟库的片源（不选 = 全部）</label>
        <div className="grid">
          {boot.sources.filter((s) => s.enabled).map((s) => (
            <label key={s.id} className="row" style={{ cursor: 'pointer', gap: 6 }}>
              <input className="check" type="checkbox"
                     checked={form.vlib_sources.includes(s.id)}
                     onChange={() => toggleId('vlib_sources', s.id)} />
              <span>{s.name || s.id}</span>
            </label>
          ))}
        </div>
        {boot.sources.filter((s) => s.enabled).length === 0 && (
          <p className="hint">当前没有启用的片源，请先在「片源配置」启用。</p>
        )}
        <label className="field mt">显示分类（不选 = 四类全开）</label>
        <div className="row">
          {VLIB_CATS.map((cat) => (
            <label key={cat.key} className="row" style={{ cursor: 'pointer', gap: 6 }}>
              <input className="check" type="checkbox"
                     checked={form.vlib_categories.includes(cat.key)}
                     onChange={() => toggleId('vlib_categories', cat.key)} />
              <span>{cat.label}</span>
            </label>
          ))}
        </div>
        <label className="field mt">库卡片形态</label>
        <div className="stack">
          <label className="row" style={{ cursor: 'pointer' }}>
            <input className="check" type="radio" name="vlib_split"
                   checked={form.vlib_split === 'source_category'}
                   onChange={() => set('vlib_split', 'source_category')} />
            <span>按源 × 分类拆分：每个源生成一组分类库卡片（如「源名 · 电影」）</span>
          </label>
          <label className="row" style={{ cursor: 'pointer' }}>
            <input className="check" type="radio" name="vlib_split"
                   checked={form.vlib_split === 'category'}
                   onChange={() => set('vlib_split', 'category')} />
            <span>只保留 4 张分类库：内容限选中源，卡片数量不变</span>
          </label>
        </div>
        <p className="hint">
          保存后会在后台立即重建分类路由，稍等片刻重新进入飞牛影视即可看到。
          卡片数量 = 选中源数 × 显示分类数，建议不超过 6 个源。
        </p>

        <div className="row end mt">
          <button className="btn primary" disabled={!!busy} onClick={() => void save()}>
            {busy === 'save' ? '保存中…' : '保存设置'}
          </button>
        </div>
      </section>

      <section className="card">
        <div className="card-title">运行状态</div>
        <div className="grid">
          <div className="stat">
            <div className="value">{status?.charts?.provider_ok ?? 0}</div>
            <div className="label">榜单数据源成功 / {status?.charts?.provider_total ?? 0}</div>
          </div>
          <div className="stat">
            <div className="value">{status?.bindings ?? 0}</div>
            <div className="label">已绑定可播放条目</div>
          </div>
          <div className="stat">
            <div className="value">{Object.values(routes).filter((r) => r.total > 0).length}</div>
            <div className="label">海量库可用分类</div>
          </div>
        </div>
        <p className="hint mt">
          榜单刷新：{timeText(status?.charts?.updated_at)}；
          分类路由：{timeText(status?.catalog?.updated_at)}
          {status?.charts?.refreshing || status?.catalog?.refreshing ? '（刷新中…）' : ''}
        </p>
        {status?.charts?.last_error && (
          <p className="hint">榜单错误：{status.charts.last_error}</p>
        )}
        <div className="stack mt">
          {Object.keys(bySource).length > 0 ? (
            <>
              <div className="row">
                <span className="badge ok">榜单四库</span>
                {VLIB_CATS.map((cat) => (
                  <span className="badge" key={cat.key}>
                    {cat.label} {chartCounts[cat.key] ?? 0}
                  </span>
                ))}
              </div>
              {Object.entries(bySource).map(([sid, cats]) => {
                const name = status?.mapping?.sources.find((s) => s.id === sid)?.name || sid;
                return (
                  <div className="row" key={sid}>
                    <span className="badge ok">{name}</span>
                    {VLIB_CATS.filter((c) => cats[c.key]).map((c) => (
                      <span className="badge" key={c.key}>
                        {c.label} {cats[c.key].total}
                      </span>
                    ))}
                  </div>
                );
              })}
            </>
          ) : VLIB_CATS.map((cat) => {
            const route = routes[cat.key];
            return (
              <div className="row" key={cat.key}>
                <span className="badge ok">榜单·{cat.label} {chartCounts[cat.key] ?? 0}</span>
                <span className="badge">
                  海量·{cat.label} {route?.total ?? 0}
                  {route?.source ? ` · ${route.source}` : ''}
                </span>
                {route?.name && <span className="small">{route.name}</span>}
              </div>
            );
          })}
        </div>
        <div className="row mt">
          <button className="btn sm" disabled={!!busy} onClick={() => void run('refresh')}>
            {busy === 'refresh' ? '刷新中…' : '立即刷新榜单与分类'}
          </button>
          <button className="btn sm" disabled={!!busy} onClick={() => void run('clear')}>
            清空虚拟库缓存
          </button>
          <span className="small">刷新失败会保留旧缓存；清空后需重新刷新才恢复内容</span>
        </div>
      </section>

      <section className="card">
        <div className="card-title">说明</div>
        <p className="small">
          虚拟库只在本扩展内伪造媒体库条目，不改官方数据库、不写官方影视数据。
          榜单条目本身不是片源，点开时才按片名搜索你配置的资源站；海量库条目来自
          资源站公开分类接口。停用扩展或关闭总开关后，媒体库首页立即恢复官方原样。
        </p>
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
