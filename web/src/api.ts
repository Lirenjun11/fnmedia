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
  search_debounce_ms: number;
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
  adfilter_head_guard: boolean;
  adfilter_breaks: boolean;
  adfilter_strong: boolean;
  vlib_enabled: boolean;
  vlib_charts: boolean;
  vlib_catalog: boolean;
  vlib_refresh_hours: number;
  acl_enabled: boolean;
  acl_subjects: AclSubject[];
  acl_grants: Record<string, AclGrant>;
  acl_devices?: Record<string, { uid: string; ua: string; ts: number }>;
}

export interface AclSubject {
  id: string;
  name: string;
  source: string;
}

export interface AclGrant {
  libs: string[];
  sources: string[];
}

export interface AclLib {
  key: string;
  title: string;
  guid: string;
  kind: string;
}

export interface AclSource {
  id: string;
  name: string;
  enabled: boolean;
}

export interface AclMe {
  uid: string;
  name: string;
  via: string;
  reason: string;
  fp: string;
  known: boolean;
}

export interface AclStatus {
  enabled: boolean;
  subjects: AclSubject[];
  grants: Record<string, AclGrant>;
  devices: { fp: string; uid: string; ua: string; ts: number }[];
  me: AclMe;
  discover: {
    db: string;
    users: number;
    tables: { db: string; user_table: string; session_tables: string[] }[];
    error: string;
  };
  libs: AclLib[];
  sources: AclSource[];
  unassigned_sources: number;
  unassigned_subjects: number;
  enabled_sources: number;
}

export interface AclSummary {
  enabled: boolean;
  known: boolean;
  uid: string;
  name: string;
  via: string;
  reason: string;
  libs: number;
  sources: number;
  sources_total: number;
}

export interface AclProbe {
  me: AclMe;
  headers: Record<string, string>;
  cookie_keys: string[];
  credential_candidates: number;
  discover: {
    db: string;
    users: { id: string; name: string; source: string }[];
    tables: { db: string; user_table: string; session_tables: string[] }[];
    error: string;
  };
}

export interface VlibRouteInfo {
  name: string;
  source: string;
  total: number;
}

export interface VlibStatus {
  enabled: boolean;
  charts_enabled: boolean;
  catalog_enabled: boolean;
  bindings: number;
  charts: {
    refreshing: boolean;
    updated_at: number;
    provider_ok: number;
    provider_total: number;
    last_error: string;
    counts: Record<string, number>;
  };
  catalog: {
    refreshing: boolean;
    updated_at: number;
    routes: Record<string, VlibRouteInfo>;
  };
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
  vlib: VlibStatus;
  acl: AclSummary;
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

export interface AdPreview {
  ts: number;
  removed: number;
  removed_duration: number;
  kept_duration: number;
  total_duration: number;
  by_marker: number;
  by_keyword: number;
  by_head: number;
  by_break: number;
  by_duplicate: number;
  by_tiny: number;
  by_minority: number;
  groups: { group: number; count: number; duration: number }[];
  disc_cleaned: number;
  guard: string;
  guard_kept: number;
  sample: string;
  dropped: { url: string; duration: number; reason: string; group: number }[];
  raw_head: string;
  kept_head: string;
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
  adfilterPreview: () =>
    request<{ previews: AdPreview[] }>('/v/api/ext/adfilter/preview'),
  vlibStatus: () => request<VlibStatus>('/v/api/ext/vlib/status'),
  vlibRefresh: (what: 'all' | 'charts' | 'catalog' = 'all') =>
    request<VlibStatus>('/v/api/ext/vlib/refresh', json('POST', { what })),
  vlibClear: () => request<VlibStatus>('/v/api/ext/vlib/cache', { method: 'DELETE' }),
  aclStatus: () => request<AclStatus>('/v/api/ext/acl/status'),
  aclProbe: () => request<AclProbe>('/v/api/ext/acl/probe'),
  aclConfig: (value: Partial<Pick<Config, 'acl_enabled' | 'acl_subjects' | 'acl_grants'>>) =>
    request<AclStatus>('/v/api/ext/acl/config', json('PATCH', value)),
  aclGrant: (uid: string, libs: string[], sources: string[], name?: string) =>
    request<AclStatus>('/v/api/ext/acl/grant', json('POST', { uid, libs, sources, name })),
  aclRevoke: (uid: string) =>
    request<AclStatus>(`/v/api/ext/acl/grant/${encodeURIComponent(uid)}`, { method: 'DELETE' }),
  aclAddSubject: (id: string, name: string) =>
    request<AclStatus>('/v/api/ext/acl/subjects', json('POST', { id, name })),
  aclDelSubject: (id: string) =>
    request<AclStatus>(`/v/api/ext/acl/subjects/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  aclBind: (uid: string) =>
    request<{ bound: boolean; fp: string; uid: string; status: AclStatus }>(
      '/v/api/ext/acl/bind', json('POST', { uid })),
  aclDiscover: () =>
    request<AclProbe['discover']>('/v/api/ext/acl/discover', json('POST', {})),
  checkUpdate: (force = false) =>
    request<UpdateInfo>(`/v/api/ext/update/check${force ? '?force=1' : ''}`),
  exportConfig: () => request<{ config: Config }>('/v/api/ext/export'),
  importConfig: (config: Config) =>
    request<{ config: Config }>('/v/api/ext/import', json('POST', { config })),
};
