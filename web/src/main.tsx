import React from 'react';
import { createRoot } from 'react-dom/client';
import { applyTheme, readTheme } from './theme';
import App from './App';
import './styles.css';

try {
  applyTheme(readTheme());
} catch {
  document.documentElement.dataset.theme = 'light';
}

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
