// I18N — lightweight translation module for Home Intercom PWA
// Supports zh-CN and en, defaults to browser language, persists choice

const I18N = (() => {
  const STORAGE_KEY = "intercom-lang";
  const UNAVAILABLE_CLASS = "unavailable";

  const LANG_OPTIONS = [
    { code: "zh-CN", label: "中文" },
    { code: "en", label: "EN" },
  ];

  const DATA = {
    "zh-CN": {
      appTitle: "家庭广播",
      appHint: "按住录音 · 松开发送",
      broadcastAll: "全部",
      statusReady: "就绪",
      statusRecording: "录音中…",
      statusSending: "发送中…",
      statusSent: "已发送",
      statusFailed: "失败",
      statusSkipped: "不支持播放",
      statusUnavailable: "离线",
      statusPlayFailed: "播放失败",
      statusMAFailed: "MA 失败",
      statusNetworkError: "网络错误",
      statusLoadFailed: "加载失败",
      micError: "麦克风: ",
      devicesTitle: "对讲按钮",
      secInsecureTitle: "麦克风不可用",
      secInsecureHint: "浏览器安全策略要求 HTTPS 连接才能使用麦克风。请通过外部 HTTPS 地址访问 Home Assistant。",
      settingsTitle: "设置",
      settingsClose: "关闭",
      chimeTitle: "自定义提示音",
      chimeDesc: "每次广播前播放",
      chimeDefault: "默认提示音",
      chimeReplace: "更换提示音",
      chimeReplaceHint: "WAV / MP3 · 最多 10 秒",
      chimeMono: "单声道",
      chimeReset: "恢复默认",
      chimePreview: "试听",
      chimeUploadOk: "提示音已保存",
      chimeUploadFail: "上传失败",
      chimeUploadTooLong: "音频过长（最多 10 秒）",
      chimePreviewFail: "无法播放",
      chimeResetOk: "已恢复默认提示音",
      themeTitle: "主题",
      themeAuto: "自动",
      themeLight: "浅色",
      themeDark: "深色",
      devicePending: "待批准",
      deviceApprove: "批准",
    },
    en: {
      appTitle: "Home Intercom",
      appHint: "Hold to record · Release to send",
      broadcastAll: "All",
      statusReady: "Ready",
      statusRecording: "Recording…",
      statusSending: "Sending…",
      statusSent: "Sent",
      statusFailed: "Failed",
      statusSkipped: "No play_media",
      statusUnavailable: "Offline",
      statusPlayFailed: "Play failed",
      statusMAFailed: "MA failed",
      statusNetworkError: "Network error",
      statusLoadFailed: "Load failed",
      micError: "Mic: ",
      devicesTitle: "Intercom Buttons",
      secInsecureTitle: "Microphone Unavailable",
      secInsecureHint: "Browser security requires HTTPS for microphone access. Please use the external HTTPS address to access Home Assistant.",
      settingsTitle: "Settings",
      settingsClose: "Close",
      chimeTitle: "Custom chime",
      chimeDesc: "Played before every announcement",
      chimeDefault: "Default chime",
      chimeReplace: "Replace chime",
      chimeReplaceHint: "WAV / MP3 · up to 10 seconds",
      chimeMono: "mono",
      chimeReset: "Reset to default",
      chimePreview: "Preview",
      chimeUploadOk: "Chime saved",
      chimeUploadFail: "Upload failed",
      chimeUploadTooLong: "Audio too long (max 10 s)",
      chimePreviewFail: "Cannot play",
      chimeResetOk: "Default chime restored",
      themeTitle: "Theme",
      themeAuto: "Auto",
      themeLight: "Light",
      themeDark: "Dark",
      devicePending: "Pending",
      deviceApprove: "Approve",
    },
  };

  // Detect from localStorage, then navigator, fallback zh-CN
  let lang = localStorage.getItem(STORAGE_KEY) || "";
  if (!lang || !DATA[lang]) {
    const nav = (navigator.language || "zh-CN").split("-")[0];
    lang = nav === "zh" ? "zh-CN" : "en";
  }

  function t(key) {
    return (DATA[lang] && DATA[lang][key]) || DATA["en"][key] || key;
  }

  function getLangLabel(code) {
    const opt = LANG_OPTIONS.find((o) => o.code === code);
    return opt ? opt.label : code;
  }

  function closeLangDropdown() {
    const root = document.getElementById("lang-dropdown");
    if (!root) return;
    root.classList.remove("open");
    const trigger = document.getElementById("lang-toggle");
    const menu = root.querySelector(".lang-dropdown-menu");
    if (trigger) trigger.setAttribute("aria-expanded", "false");
    if (menu) menu.hidden = true;
  }

  function updateLangDropdown() {
    const label = document.getElementById("lang-toggle-label");
    if (label) label.textContent = getLangLabel(lang);

    document.querySelectorAll(".lang-dropdown-menu [data-lang]").forEach((btn) => {
      const active = btn.dataset.lang === lang;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
  }

  function initLangDropdown() {
    const root = document.getElementById("lang-dropdown");
    if (!root || root.dataset.bound) return;
    root.dataset.bound = "1";

    const trigger = document.getElementById("lang-toggle");
    const menu = root.querySelector(".lang-dropdown-menu");
    if (!trigger || !menu) return;

    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      if (typeof THEME !== "undefined" && typeof THEME.closeDropdown === "function") THEME.closeDropdown();
      const open = !root.classList.contains("open");
      root.classList.toggle("open", open);
      trigger.setAttribute("aria-expanded", open ? "true" : "false");
      menu.hidden = !open;
    });

    menu.querySelectorAll("[data-lang]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        setLang(btn.dataset.lang);
        closeLangDropdown();
      });
    });

    document.addEventListener("click", closeLangDropdown);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeLangDropdown();
    });
  }

  function setLang(newLang) {
    if (!DATA[newLang]) return;
    lang = newLang;
    localStorage.setItem(STORAGE_KEY, lang);
    applyToDOM();
  }

  function toggleLang() {
    setLang(lang === "zh-CN" ? "en" : "zh-CN");
  }

  function applyToDOM() {
    document.documentElement.lang = lang;

    const h1 = document.querySelector(".header h1");
    if (h1) h1.textContent = t("appTitle");
    const hint = document.querySelector(".header .hint");
    if (hint) hint.textContent = t("appHint");
    updateLangDropdown();

    const bcName = document.querySelector('[data-i18n="broadcastAll"]');
    if (bcName) bcName.textContent = t("broadcastAll");

    document.querySelectorAll("[data-room-name]").forEach((el) => {
      const key = el.getAttribute("data-room-name");
      const room = window._ROOM_DATA ? window._ROOM_DATA[key] : null;
      if (!room) return;
      el.textContent = lang === "en" && room.name_en ? room.name_en : room.name;
    });

    document.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(el.getAttribute("data-i18n"));
    });

    // Re-render the device list with the new language (room names change)
    if (typeof window.renderDevices === "function") window.renderDevices();

    document.title = t("appTitle");

    document.querySelectorAll(".room-card .status").forEach((el) => {
      const card = el.closest(".room-card");
      if (card && card.classList.contains(UNAVAILABLE_CLASS)) return;

      const val = el.textContent;
      let key = null;
      for (const k of Object.keys(DATA["zh-CN"])) {
        if (DATA["zh-CN"][k] === val || DATA["en"][k] === val) {
          key = k;
          break;
        }
      }
      if (key) el.textContent = t(key);
    });

    if (typeof window.updateChimeStatusUI === "function") window.updateChimeStatusUI();

    const settingsToggle = document.getElementById("settings-toggle");
    const settingsClose = document.getElementById("settings-close");
    if (settingsToggle) settingsToggle.setAttribute("aria-label", t("settingsTitle"));
    if (settingsClose) settingsClose.setAttribute("aria-label", t("settingsClose"));
    if (typeof THEME !== "undefined" && typeof THEME.updateDropdown === "function") THEME.updateDropdown();
  }

  function init() {
    const run = () => {
      initLangDropdown();
      applyToDOM();
    };
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", run);
    } else {
      run();
    }
  }

  return { t, setLang, toggleLang, init, closeLangDropdown, get lang() { return lang; } };
})();

I18N.init();

const THEME = (() => {
  const STORAGE_KEY = "intercom-theme";
  const OPTIONS = ["auto", "light", "dark"];
  const COLORS = { dark: "#0f0f0f", light: "#f3f3f4" };
  const LABEL_KEYS = { auto: "themeAuto", light: "themeLight", dark: "themeDark" };

  let pref = localStorage.getItem(STORAGE_KEY) || "auto";
  if (!OPTIONS.includes(pref)) pref = "auto";

  const media = window.matchMedia("(prefers-color-scheme: dark)");

  function resolved() {
    if (pref === "light" || pref === "dark") return pref;
    return media.matches ? "dark" : "light";
  }

  function apply() {
    const theme = resolved();
    document.documentElement.setAttribute("data-theme", theme);
    document.documentElement.setAttribute("data-theme-pref", pref);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", COLORS[theme]);
    updateDropdown();
    if (typeof window.updateChimeStatusUI === "function") window.updateChimeStatusUI();
  }

  function setPref(next) {
    if (!OPTIONS.includes(next)) return;
    pref = next;
    localStorage.setItem(STORAGE_KEY, pref);
    apply();
  }

  function closeDropdown() {
    const root = document.getElementById("theme-dropdown");
    if (!root) return;
    root.classList.remove("open");
    const trigger = document.getElementById("theme-toggle");
    const menu = root.querySelector(".lang-dropdown-menu");
    if (trigger) trigger.setAttribute("aria-expanded", "false");
    if (menu) menu.hidden = true;
  }

  function updateDropdown() {
    const trigger = document.getElementById("theme-toggle");
    if (trigger && typeof I18N !== "undefined") {
      trigger.setAttribute("aria-label", I18N.t("themeTitle") + " — " + I18N.t(LABEL_KEYS[pref]));
    }

    document.querySelectorAll("#theme-dropdown [data-theme-pref]").forEach((btn) => {
      const active = btn.dataset.themePref === pref;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
  }

  function initDropdown() {
    const root = document.getElementById("theme-dropdown");
    if (!root || root.dataset.bound) return;
    root.dataset.bound = "1";

    const trigger = document.getElementById("theme-toggle");
    const menu = root.querySelector(".lang-dropdown-menu");
    if (!trigger || !menu) return;

    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      if (typeof I18N.closeLangDropdown === "function") I18N.closeLangDropdown();
      const open = !root.classList.contains("open");
      root.classList.toggle("open", open);
      trigger.setAttribute("aria-expanded", open ? "true" : "false");
      menu.hidden = !open;
    });

    menu.querySelectorAll("[data-theme-pref]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        setPref(btn.dataset.themePref);
        closeDropdown();
      });
    });

    document.addEventListener("click", closeDropdown);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeDropdown();
    });
  }

  function onMediaChange() {
    if (pref === "auto") apply();
  }

  function init() {
    const run = () => {
      initDropdown();
      apply();
    };
    if (media.addEventListener) media.addEventListener("change", onMediaChange);
    else if (media.addListener) media.addListener(onMediaChange);
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", run);
    } else {
      run();
    }
  }

  return { init, setPref, apply, closeDropdown, updateDropdown, get pref() { return pref; } };
})();

THEME.init();

