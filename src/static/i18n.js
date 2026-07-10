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
      statusNetworkError: "网络错误",
      statusLoadFailed: "加载失败",
      micError: "麦克风: ",
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
      statusNetworkError: "Network error",
      statusLoadFailed: "Load failed",
      micError: "Mic: ",
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

  return { t, setLang, toggleLang, init, get lang() { return lang; } };
})();

I18N.init();
