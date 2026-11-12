export interface Source {
  id: string;
  name: string;
  key?: string;
  api: string;
  detail?: string;
  adult?: boolean;
  enabled: boolean;
  from: string;
}

export interface ProbeResult {
  id: string;
  ok: boolean;
  count: number;
  elapsed_ms?: number;
  error?: string;
}

export interface Config {
  sources: Source[];
  subscriptions: { url: string; enabled: boolean; last_sync?: number }[];
  subscription_url: string;
  auto_update: boolean;
  update_interval_hours: number;
  config_text: string;
  last_update: number;
  enabled_limit: number;
  search_timeout: number;
  search_concurrency: number;
  play_timeout: number;
  online_first: boolean;
  merge_sources: boolean;
  borrow_official: boolean;
  auto_best_source: boolean;
  allow_intranet: boolean;
  douban_enabled: boolean;
  adfilter_enabled: boolean;
  adfilter_markers: boolean;
  adfilter_keywords: string[];
  adfilter_head_skip: number;
}

export interface Bootstrap {
  version: string;
  uptime: number;
  online_enabled: boolean;
  sources_total: number;
  sources_enabled: number;
  titles_cached: number;
  last_update: number;
  config: Config;
  sources: Source[];
}

export interface UpdateInfo {
  current: string;
  latest: string;
  has_update: boolean;
  html_url: string;
  notes: string;
  assets: { name: string; url: string }[];
  published_at: string;
  error: string;
  cached: boolean;
}

export interface FetchReport {
  total: number;
  added: number;
  duplicated: number;
  skipped: number;
  adult?: number;
  skipped_reasons: Record<string, number>;
  new_sources: number;
  sources_total: number;
  enabled: number;
  url: string;
}

const TOKEN =
  document.querySelector<HTMLMetaElement>('meta[name="fnos-token"]')?.content || '';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const writing = !!init?.method && !['GET', 'HEAD'].includes(init.method.toUpperCase());
  const res = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: {
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...(writing && TOKEN ? { 'X-Admin-Token': TOKEN } : {}),
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    let message = `请求失败 (${res.status})`;
    try {
      const body = await res.json();
      message = body.msg || body.detail || message;
    } catch {  }
    throw new Error(message);
  }
  const body = await res.json();
  if (body.code !== 0) throw new Error(body.msg || '操作失败');
  return body.data as T;
}

const json = (method: string, value: unknown): RequestInit => ({
  method,
  body: JSON.stringify(value),
});

export const api = {
  bootstrap: () => request<Bootstrap>('/v/api/ext/bootstrap'),
  patchConfig: (value: Partial<Config>) =>
    request<{ config: Config }>('/v/api/ext/config', json('PATCH', value)),
  addSource: (value: { name: string; api: string; key?: string; detail?: string }) =>
    request<{ source: Source }>('/v/api/ext/sources', json('POST', value)),
  batchSources: (ids: string[], action: 'enable' | 'disable' | 'delete') =>
    request<{ changed: number; action: string; total: number }>(
      '/v/api/ext/sources/batch', json('POST', { ids, action })),
  probeSources: (ids: string[], keyword: string) =>
    request<{
      results: ProbeResult[];
      total: number;
      done: number;
      timed_out: number;
      budget_ms: number;
      elapsed_ms: number;
    }>('/v/api/ext/sources/probe', json('POST', { ids, keyword })),
  updateSource: (id: string, value: Partial<Source>) =>
    request<{ source: Source }>(`/v/api/ext/sources/${encodeURIComponent(id)}`, json('PATCH', value)),
  deleteSource: (id: string) =>
    request<{ deleted: string }>(`/v/api/ext/sources/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  testSource: (id: string) =>
    request<{ ok: boolean; count: number; elapsed_ms?: number; error?: string }>(
      `/v/api/ext/sources/${encodeURIComponent(id)}/test`, json('POST', {})),
  fetchSubscription: (url?: string) =>
    request<FetchReport>('/v/api/ext/subscriptions/fetch', json('POST', url ? { url } : {})),
  saveSubscriptionSettings: (value: {
    subscription_url?: string;
    auto_update?: boolean;
    update_interval_hours?: number;
  }) => request<{
    subscription_url: string;
    auto_update: boolean;
    update_interval_hours: number;
    last_update: number;
  }>('/v/api/ext/subscriptions/settings', json('PATCH', value)),
  saveSubscriptionContent: (text: string) =>
    request<FetchReport>('/v/api/ext/subscriptions/content', json('POST', { text })),
  clearCache: () => request<{ cleared: number }>('/v/api/ext/cache', { method: 'DELETE' }),
  logs: () => request<{ logs: { ts: number; msg: string }[] }>('/v/api/ext/logs'),
  checkUpdate: (force = false) =>
    request<UpdateInfo>(`/v/api/ext/update/check${force ? '?force=1' : ''}`),
  exportConfig: () => request<{ config: Config }>('/v/api/ext/export'),
  importConfig: (config: Config) =>
    request<{ config: Config }>('/v/api/ext/import', json('POST', { config })),
};
