import { useEffect, useState } from 'react';
import { api, type Bootstrap, type FetchReport } from './api';
import type { Notify } from './App';

interface Props {
  boot: Bootstrap;
  refresh: () => Promise<void>;
  notify: Notify;
}

export default function SubscriptionsPage({ boot, refresh, notify }: Props) {
  const [subUrl, setSubUrl] = useState(boot.config.subscription_url || '');
  const [autoUpdate, setAutoUpdate] = useState(!!boot.config.auto_update);
  const [intervalHours, setIntervalHours] = useState(boot.config.update_interval_hours || 24);
  const [configText, setConfigText] = useState(boot.config.config_text || '');
  const [report, setReport] = useState<FetchReport | null>(null);
  const [busy, setBusy] = useState('');

  useEffect(() => {
    setSubUrl(boot.config.subscription_url || '');
    setAutoUpdate(!!boot.config.auto_update);
    setIntervalHours(boot.config.update_interval_hours || 24);
    setConfigText(boot.config.config_text || '');
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

  async function doFetch() {
    await run('fetch', async () => {
      const result = await api.fetchSubscription(subUrl.trim() || undefined);
      setReport(result);
      notify(`全量拉取完成：启用 ${result.enabled} 个源，共登记 ${result.sources_total} 个`, 'ok');
      await refresh();
    });
  }

  async function doSaveSettings() {
    await run('settings', async () => {
      await api.saveSubscriptionSettings({
        subscription_url: subUrl.trim(),
        auto_update: autoUpdate,
        update_interval_hours: Number(intervalHours),
      });
      notify('订阅设置已保存', 'ok');
      await refresh();
    });
  }

  async function doApplyContent() {
    await run('content', async () => {
      if (!configText.trim()) throw new Error('配置内容为空');
      const result = await api.saveSubscriptionContent(configText);
      setReport(result);
      notify(`配置已全量应用：启用 ${result.enabled} 个源`, 'ok');
      await refresh();
    });
  }

  const lastUpdate = boot.last_update
    ? new Date(boot.last_update * 1000).toLocaleString()
    : '从未';

  return (
    <>
      <div className="page-head">
        <h1>订阅配置</h1>
        <p className="page-desc">
          整份配置全量拉取，所有可识别的源一次性导入并启用；手工添加的源会保留。
        </p>
      </div>

      {boot.sources_total === 0 && (
        <div className="empty mb">
          <div className="empty-icon">◈</div>
          <h2>还没有任何源</h2>
          <p>
            当前为纯透传状态，官方影视的搜索、媒体库与播放完全不受影响。
            拉取一份配置订阅后，在线结果才会出现在原生搜索里。
          </p>
          <span className="badge ok">零源是合法的默认状态</span>
        </div>
      )}

      <section className="card">
        <div className="card-title" style={{ justifyContent: 'space-between' }}>
          <span>配置订阅</span>
          <span className="small">最后更新：{lastUpdate}</span>
        </div>
        <p className="card-sub">
          支持 Base58 编码的 api_site 配置、明文 JSON 配置；
          拉取后整份替换订阅源，不做勾选筛选。
        </p>

        <label className="field">订阅 URL</label>
        <input
          type="url"
          value={subUrl}
          placeholder="粘贴你自己持有的配置订阅地址"
          onChange={(e) => setSubUrl(e.target.value)}
        />
        <p className="hint">拉取超时 5 秒，大小上限 512 KB，不占用搜索与播放的连接池。</p>
        <button className="btn success wide mt" disabled={busy === 'fetch'} onClick={() => void doFetch()}>
          {busy === 'fetch' ? '拉取中…' : '拉取配置'}
        </button>

        <div className="divider" />
        <div className="row">
          <div className="grow">
            <div className="card-title">自动更新</div>
            <p className="hint">需填写订阅 URL；开启后按间隔自动拉取，失败保留旧配置。本地文件方式请手动重新拉取。</p>
          </div>
          <label className="switch">
            <input type="checkbox" checked={autoUpdate}
                   onChange={(e) => setAutoUpdate(e.target.checked)} />
            <span className="slider" />
          </label>
        </div>
        <div className="row mt">
          <label className="field" style={{ margin: 0 }}>更新间隔（小时）</label>
          <input
            type="number" min="1" max="168" value={intervalHours}
            style={{ width: 110 }}
            onChange={(e) => setIntervalHours(Number(e.target.value))}
          />
          <button className="btn sm" disabled={busy === 'settings'} onClick={() => void doSaveSettings()}>
            {busy === 'settings' ? '保存中…' : '保存订阅设置'}
          </button>
          <span className="small">
            {autoUpdate ? `已开启，每 ${intervalHours} 小时刷新一次` : '已关闭，仅手动拉取'}
          </span>
        </div>

        <div className="divider" />
        <div className="card-title">配置内容</div>
        <p className="card-sub">
          显示最近一次拉取解码后的配置，可直接编辑后保存并应用。完整配置备份请在「关于」页用文件导入导出。
        </p>
        <textarea
          value={configText}
          placeholder="拉取订阅后这里会显示配置 JSON，也可以直接粘贴编辑"
          onChange={(e) => setConfigText(e.target.value)}
          style={{ minHeight: 150 }}
        />
        <div className="row mt">
          <button className="btn primary sm" disabled={busy === 'content'}
                  onClick={() => void doApplyContent()}>
            {busy === 'content' ? '应用中…' : '保存并应用'}
          </button>
          <span className="small">编辑内容不会立即生效，需点击保存并应用</span>
        </div>

        {report && (
          <div className="pill-row mt">
            <span className="badge ok">启用 {report.enabled}</span>
            <span className="pill">识别 {report.total} 条</span>
            <span className="pill">新增 {report.new_sources}</span>
            <span className="pill">跳过 {report.skipped}</span>
            {!!report.adult && <span className="pill">特殊标记 {report.adult} 个（默认禁用）</span>}
            <span className="pill">重复 {report.duplicated}</span>
            <span className="pill">当前共 {report.sources_total} 个源</span>
            {Object.entries(report.skipped_reasons).map(([reason, n]) => (
              <span className="pill" key={reason}>{reason} {n}</span>
            ))}
          </div>
        )}
      </section>
    </>
  );
}
