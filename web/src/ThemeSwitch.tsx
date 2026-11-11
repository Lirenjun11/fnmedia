import { THEME_OPTIONS, useTheme } from './theme';

export default function ThemeSwitch() {
  const [theme, setTheme] = useTheme();
  return (
    <div className="theme-switch" role="group" aria-label="外观主题">
      {THEME_OPTIONS.map((option) => (
        <button
          key={option.key}
          className={`theme-seg ${theme === option.key ? 'active' : ''}`}
          onClick={() => setTheme(option.key)}
        >
          <span className="theme-ico">{option.icon}</span>
          <span>{option.label}</span>
        </button>
      ))}
    </div>
  );
}
