import { useEffect, useState } from 'react';

export type Theme = 'light' | 'dark' | 'system';

export const THEME_OPTIONS: { key: Theme; label: string; icon: string }[] = [
  { key: 'light', label: '浅色', icon: '☀' },
  { key: 'dark', label: '深色', icon: '☾' },
  { key: 'system', label: '跟随系统', icon: '◐' },
];

const KEY = 'fnos_theme';
let media: MediaQueryList | null = null;
let current: Theme = 'system';
const listeners = new Set<(theme: Theme) => void>();

function storageGet(key: string): string | null {
  try {
    if (typeof localStorage === 'undefined') return null;
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(key: string, value: string): void {
  try {
    if (typeof localStorage === 'undefined') return;
    localStorage.setItem(key, value);
  } catch {
    return;
  }
}

export function readTheme(): Theme {
  const saved = storageGet(KEY);
  return saved === 'light' || saved === 'dark' || saved === 'system'
    ? saved
    : 'system';
}

export function onThemeChange(fn: (theme: Theme) => void): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

export function applyTheme(theme: Theme): void {
  current = theme;
  storageSet(KEY, theme);
  if (!media && typeof window.matchMedia === 'function') {
    media = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = () => applyTheme(current);
    if (media.addEventListener) media.addEventListener('change', handler);
    else media.addListener(handler);
  }
  const dark = theme === 'dark'
    || (theme === 'system' && (media?.matches ?? false));
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  listeners.forEach((fn) => fn(theme));
}

export function useTheme(): [Theme, (theme: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(readTheme);
  useEffect(() => {
    applyTheme(readTheme());
    return onThemeChange(setTheme);
  }, []);
  return [theme, (next: Theme) => applyTheme(next)];
}
