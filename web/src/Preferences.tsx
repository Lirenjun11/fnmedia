import { useEffect, useState } from 'react';
import { api, type Bootstrap } from './api';
import ThemeSwitch from './ThemeSwitch';
import type { Notify } from './App';

interface Props {
  boot: Bootstrap;
  refresh: () => Promise<void>;
  notify: Notify;
}

export default function PreferencesPage({ boot, refresh, notify }: Props) {
  const [importFile, setImportFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [douban, setDouban] = useState(!!boot.config.douban_enabled);
  const [borrow, setBorrow] = useState(!!boot.config.borrow_official);
  const [intranet, setIntranet] = useState(!!boot.config.allow_intranet);

  useEffect(() => {
    setDouban(!!boot.config.douban_enabled);
    setBorrow(!!boot.config.borrow_official);
    setIntranet(!!boot.config.allow_intranet);
  }, [boot]);

  async function saveIntranet() {
    setBusy(true);
    try {
      await api.patchConfig({ allow_intranet: intranet });
      await refresh();
      notify(intranet ? '已允许内网自建源' : '已限制为仅公网目标', 'ok');
    } catch (err) {
      notify(err instanceof Error ? err.message : '保存失败', 'err');
    } finally {
      setBusy(false);
    }
  }

  async function saveBorrow() {
    setBusy(true);
    try {
      await api.patchConfig({ borrow_official: borrow });
      await refresh();
      notify(borrow ? '官方元数据借用已开启' : '官方元数据借用已关闭', 'ok');
    } catch (err) {
      notify(err instanceof Error ? err.message : '保存失败', 'err');
    } finally {
      setBusy(false);
    }
  }

  async function saveDouban() {
    setBusy(true);
    try {
      await api.patchConfig({ douban_enabled: douban });
      await refresh();
      notify(douban ? '豆瓣增强已开启' : '豆瓣增强已关闭', 'ok');
    } catch (err) {
      notify(err instanceof Error ? err.message : '保存失败', 'err');
    } finally {
      setBusy(false);
    }
  }

  async function exportConfig() {
    try {
      const data = await api.exportConfig();
      const blob = new Blob([JSON.stringify(data.config, null, 2)], { type: 'application/json' });
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `fnmedia-config-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(link.href);
      notify('配置文件已下载到本地', 'ok');
    } catch (err) {
      notify(err instanceof Error ? err.message : '导出失败', 'err');
    }
  }

  async function importConfig() {
    if (!importFile) return;
    if (!window.confirm(`导入「${importFile.name}」会覆盖当前的源与订阅设置，继续？`)) return;
    setBusy(true);
    try {
      const parsed = JSON.parse(await importFile.text());
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('配置文件格式无效');
      }
      await api.importConfig(parsed);
      setImportFile(null);
      await refresh();
      notify('配置已导入并热加载', 'ok');
    } catch (err) {
      notify(err instanceof Error ? err.message : '导入失败', 'err');
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>设置</h1>
        <p className="page-desc">外观主题与配置备份</p>
      </div>

      <section className="card">
        <div className="card-title">外观主题</div>
        <p className="card-sub">浅色 / 深色 / 跟随系统；跟随系统会随设备主题变化实时切换。</p>
        <div style={{ maxWidth: 320 }}><ThemeSwitch /></div>
      </section>

      <section className="card">
        <div className="card-title">内网自建源</div>
        <p className="card-sub">
          开启后，订阅地址、海报、HLS 分片与直链允许指向局域网或回环地址
          （例如 NAS 本机、192.168.x.x、10.x.x.x）。链路本地与云元数据地址始终拦截。
          关闭后仅允许公网目标，可避免本服务被当作内网探测代理。
        </p>
        <div className="row">
          <label className="switch">
            <input
              type="checkbox" checked={intranet}
              onChange={(e) => setIntranet(e.target.checked)}
            />
            <span className="slider" />
          </label>
          <button className="btn sm" disabled={busy} onClick={() => void saveIntranet()}>
            {busy ? '保存中…' : '保存'}
          </button>
          <span className="small">{intranet ? '已允许内网目标' : '仅公网目标'}</span>
        </div>
      </section>

      <section className="card">
        <div className="card-title">官方元数据借用</div>
        <p className="card-sub">
          在线条目与本地媒体库刮削结果按「标题 + 年份」关联，命中后使用官方的
          横版背景图、带头像演职人员、时长与 IMDb 链接。只读访问官方数据库，
          不产生任何网络请求。默认开启。
        </p>
        <div className="row">
          <label className="switch">
            <input
              type="checkbox" checked={borrow}
              onChange={(e) => setBorrow(e.target.checked)}
            />
            <span className="slider" />
          </label>
          <button className="btn sm" disabled={busy} onClick={() => void saveBorrow()}>
            {busy ? '保存中…' : '保存'}
          </button>
          <span className="small">{borrow ? '已开启' : '已关闭'}</span>
        </div>
      </section>

      <section className="card">
        <div className="card-title">豆瓣元数据增强</div>
        <p className="card-sub">
          开启后，点开影片详情时按需访问豆瓣获取更清晰的竖版海报；
          仅对你点开的影片生效，失败静默回退，本地库已命中的片优先使用官方刮削数据。
        </p>
        <div className="row">
          <label className="switch">
            <input
              type="checkbox" checked={douban}
              onChange={(e) => setDouban(e.target.checked)}
            />
            <span className="slider" />
          </label>
          <button className="btn sm" disabled={busy} onClick={() => void saveDouban()}>
            {busy ? '保存中…' : '保存'}
          </button>
          <span className="small">{douban ? '已开启' : '已关闭，零外连'}</span>
        </div>
      </section>

      <section className="card">
        <div className="card-title">配置备份</div>
        <p className="card-sub">
          导出为本地 JSON 文件（不含管理令牌）；导入时选择此前导出的文件，会覆盖当前源与订阅设置。
        </p>
        <div className="row mb">
          <button className="btn" onClick={() => void exportConfig()}>导出配置文件</button>
        </div>
        <div className="divider" />
        <div className="card-title">导入配置</div>
        <div className="row mt">
          <label className="btn sm">
            选择 .json 文件
            <input
              type="file"
              accept=".json,application/json"
              style={{ display: 'none' }}
              onChange={(e) => setImportFile(e.target.files?.[0] ?? null)}
            />
          </label>
          {importFile && (
            <span className="small">
              {importFile.name} · {(importFile.size / 1024).toFixed(1)} KB
            </span>
          )}
          <button className="btn primary sm" disabled={!importFile || busy}
                  onClick={() => void importConfig()}>
            {busy ? '导入中…' : '导入并覆盖'}
          </button>
        </div>
      </section>
    </>
  );
}
