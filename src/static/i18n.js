// I18N — lightweight translation module for Home Intercom PWA
// Supports zh-CN and en, defaults to browser language, persists choice

const I18N = (() => {
  const STORAGE_KEY = "intercom-lang";

  const DATA = {
    "zh-CN": {
      appTitle: "📢 家庭广播",
      appHint: "按住录音 · 松开发送",
      broadcastAll: "全部广播",
      statusReady: "就绪",
      statusRecording: "录音中…",
      statusSending: "发送中…",
      statusSent: "已发送",
      statusFailed: "失败",
      statusSkipped: "不支持播放",
      statusUnavailable: "离线",
      statusNetworkError: "网络错误",
      statusLoadFailed: "加载失败",
      micError: "❌ 麦克风: ",
      langLabel: "EN",
    },
    en: {
      appTitle: "📢 Home Intercom",
      appHint: "Hold to record · Release to send",
      broadcastAll: "Broadcast All",
      statusReady: "Ready",
      statusRecording: "Recording…",
      statusSending: "Sending…",
      statusSent: "Sent",
      statusFailed: "Failed",
      statusSkipped: "No play_media",
      statusUnavailable: "Offline",
      statusNetworkError: "Network error",
      statusLoadFailed: "Load failed",
      micError: "❌ Mic: ",
      langLabel: "中文",
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
    // Title + hint
    const h1 = document.querySelector(".header h1");
    if (h1) h1.textContent = t("appTitle");
    const hint = document.querySelector(".header .hint");
    if (hint) hint.textContent = t("appHint");
    const langBtn = document.getElementById("lang-toggle");
    if (langBtn) langBtn.textContent = t("langLabel");

    // Broadcast card name — uses data-i18n attribute
    const bcName = document.querySelector('[data-i18n="broadcastAll"]');
    if (bcName) bcName.textContent = t("broadcastAll");

    // Room names — use name_en from rooms.json when in English mode
    document.querySelectorAll("[data-room-name]").forEach((el) => {
      const key = el.getAttribute("data-room-name");
      const room = window._ROOM_DATA ? window._ROOM_DATA[key] : null;
      if (!room) return;
      el.textContent = lang === "en" && room.name_en ? room.name_en : room.name;
    });

    // Page title
    document.title = t("appTitle").replace(/^📢 /, "");

    // Status text — keep state, just translate to current language
    // Skip unavailable cards; pollSpeakerStatus owns their text
    document.querySelectorAll(".room-card .status").forEach((el) => {
      const card = el.closest(".room-card");
      if (card && card.classList.contains("unavailable")) return;

      // Find which key the current text corresponds to (any language)
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

  // Apply translations on DOM ready
  function init() {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", applyToDOM);
    } else {
      applyToDOM();
    }
  }

  return { t, setLang, toggleLang, init, get lang() { return lang; } };
})();

I18N.init();
