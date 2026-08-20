/* SAM 看板 · 配色即时套用（贴进 Console 回车即可，刷新失效）*/
(() => {
  const KEYS = ['--primary','--green','--amber','--red','--violet','--cyan','--sidebar-bg','--bg','--surface','--text-1'];
  const before = {}; KEYS.forEach(k => before[k] = getComputedStyle(document.documentElement).getPropertyValue(k).trim());
  console.log('%c当前配色（改之前）','font-weight:700'); console.table(before);

  const TARGET = {
    "--bg": "#F4F6F9",
    "--bg-subtle": "#EAECF0",
    "--surface": "#FFFFFF",
    "--surface-2": "#F8F9FB",
    "--surface-3": "#EEF0F4",
    "--surface-4": "#E4E7ED",
    "--border": "rgba(0,0,0,0.08)",
    "--border-2": "rgba(0,0,0,0.14)",
    "--border-3": "rgba(0,0,0,0.2)",
    "--text-1": "#0D1117",
    "--text-2": "#4B5563",
    "--text-3": "#9CA3AF",
    "--text-inv": "#FFFFFF",
    "--text-sidebar": "rgba(255,255,255,0.9)",
    "--sidebar-bg": "#0F1117",
    "--sidebar-surface": "rgba(255,255,255,0.06)",
    "--sidebar-border": "rgba(255,255,255,0.08)",
    "--sidebar-active": "rgba(99,102,241,0.2)",
    "--sidebar-active-border": "#6366F1",
    "--primary": "#4F46E5",
    "--primary-hover": "#4338CA",
    "--primary-light": "#EEF2FF",
    "--primary-mid": "rgba(79,70,229,0.12)",
    "--primary-glow": "rgba(99,102,241,0.35)",
    "--green": "#10B981",
    "--green-light": "#ECFDF5",
    "--green-mid": "rgba(16,185,129,0.12)",
    "--amber": "#F59E0B",
    "--amber-light": "#FFFBEB",
    "--amber-mid": "rgba(245,158,11,0.12)",
    "--red": "#EF4444",
    "--red-light": "#FEF2F2",
    "--red-mid": "rgba(239,68,68,0.12)",
    "--violet": "#8B5CF6",
    "--violet-light": "#F5F3FF",
    "--violet-mid": "rgba(139,92,246,0.12)",
    "--cyan": "#06B6D4",
    "--cyan-light": "#ECFEFF",
    "--shadow-xs": "0 1px 2px rgba(0,0,0,0.05)",
    "--shadow-sm": "0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.04)",
    "--shadow-md": "0 4px 16px rgba(0,0,0,0.08), 0 2px 4px rgba(0,0,0,0.04)",
    "--shadow-lg": "0 16px 48px rgba(0,0,0,0.12), 0 4px 12px rgba(0,0,0,0.06)",
    "--shadow-xl": "0 32px 80px rgba(0,0,0,0.16), 0 8px 24px rgba(0,0,0,0.08)"
  };

  Object.entries(TARGET).forEach(([k,v]) => document.documentElement.style.setProperty(k,v));
  console.log('%c已套用目标配色 ✅','color:#10B981;font-weight:700');

  const diff = KEYS.filter(k => before[k].toLowerCase() !== (TARGET[k]||'').toLowerCase());
  console.log('%c有差异的 token（这些就是要落到文件里的）','font-weight:700');
  console.table(diff.map(k => ({ token:k, 你的:before[k], 目标:TARGET[k] })));
})();
