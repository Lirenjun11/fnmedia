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
  ui_theme: 'light' | 'dark' | 'system';
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
  vlib_sources: string[];
  vlib_categories: string[];
  vlib_class_ids: Record<string, string[]>;
  vlib_split: string;
  acl_enabled: boolean;
  acl_subjects: AclSubject[];
  acl_grants: Record<string, AclGrant>;
}

export interface AclSubject {
  id: string;
  name: string;
  source: string;
  is_admin?: boolean;
}

export interface AclGrant {
  // 键不存在 = 该账号未单独配置（按默认策略）；空数组 = 明确一个都不给。
  libs?: string[];
  sources?: string[];
  online?: boolean;
}

export interface AclLib {
  key: string;
  title: string;
  guid: string;
  kind: string;
  group?: string;
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
  defaults: { visible: boolean; online: boolean };
  subjects: AclSubject[];
  grants: Record<string, AclGrant>;
  me: AclMe;
  discover: {
    db: string;
    users: number;
    tables: { db: string; user_table: string; session_tables: string[] }[];
    sessions: number;
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
    users: { id: string; name: string; source: string; is_admin?: boolean }[];
    tables: { db: string; user_table: string; session_tables: string[] }[];
    sessions: { db: string; table: string; token: string; uid: string }[];
    error: string;
  };
}

export interface VlibRouteInfo {
  name: string;
  source: string;
  total: number;
  mdb?: string;
}

export interface VlibClass {
  id: string;
  name: string;
  junk: boolean;
  mdb: string;
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
    by_source?: Record<string, Record<string, VlibRouteInfo>>;
  };
  mapping?: {
    configured_sources: string[];
    sources: { id: string; name: string }[];
    categories: string[];
    class_ids?: Record<string, string[]>;
    classes_loaded?: Record<string, number>;
    split: string;
    configured_split: string;
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
  setTheme: (theme: 'light' | 'dark' | 'system') =>
    request<{ theme: string }>('/v/api/ext/ui/theme', json('PATCH', { theme })),
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
  vlibClasses: (sids: string[], force = false) =>
    request<{ classes: Record<string, VlibClass[]>; loaded: string[] }>(
      `/v/api/ext/vlib/classes?sids=${encodeURIComponent(sids.join(','))}${force ? '&force=1' : ''}`),
  vlibRefresh: (what: 'all' | 'charts' | 'catalog' = 'all') =>
    request<VlibStatus>('/v/api/ext/vlib/refresh', json('POST', { what })),
  vlibClear: () => request<VlibStatus>('/v/api/ext/vlib/cache', { method: 'DELETE' }),
  aclStatus: () => request<AclStatus>('/v/api/ext/acl/status'),
  aclProbe: () => request<AclProbe>('/v/api/ext/acl/probe'),
  // 后端 PATCH /acl/config 的开关字段叫 enabled（配置键是 acl_enabled）。
  // 之前这里发 acl_enabled 会被后端忽略，接口仍回 200，表现是「勾了又弹回」。
  aclConfig: (value: {
    enabled?: boolean; acl_subjects?: unknown; acl_grants?: unknown;
    acl_default_visible?: boolean; acl_default_online?: boolean;
  }) =>
    request<AclStatus>('/v/api/ext/acl/config', json('PATCH', value)),
  aclGrant: (uid: string, libs: string[], sources: string[], name?: string,
    online = true) =>
    request<AclStatus>('/v/api/ext/acl/grant',
      json('POST', { uid, libs, sources, name, online })),
  aclRevoke: (uid: string) =>
    request<AclStatus>(`/v/api/ext/acl/grant/${encodeURIComponent(uid)}`, { method: 'DELETE' }),
  aclAddSubject: (id: string, name: string) =>
    request<AclStatus>('/v/api/ext/acl/subjects', json('POST', { id, name })),
  aclDelSubject: (id: string) =>
    request<AclStatus>(`/v/api/ext/acl/subjects/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  aclDiscover: () =>
    request<AclProbe['discover']>('/v/api/ext/acl/discover', json('POST', {})),
  checkUpdate: (force = false) =>
    request<UpdateInfo>(`/v/api/ext/update/check${force ? '?force=1' : ''}`),
  exportConfig: () => request<{ config: Config }>('/v/api/ext/export'),
  importConfig: (config: Config) =>
    request<{ config: Config }>('/v/api/ext/import', json('POST', { config })),
};
