// Lets the extension's dashboard pages run as a normal website: stands in for the chrome.* APIs
// they use, reading the data the server publishes to data.json.
// Read/unread state is shared across all your devices through the server (/api/read).
// Syncing and phone-alert settings stay in the extension.
(() => {
  const PORTAL = 'https://campus.placements.iitb.ac.in/applicant/home';
  const listeners = [];
  let data = {};
  let read = [];
  let hidden = [];
  let syncing = false;
  let dataTag = ''; // the server's fingerprint (ETag) of the data last loaded

  const readIds = () => read;
  // Hidden companies, shared by all your devices.
  const syncHidden = async (hide = [], unhide = []) => {
    try {
      const r = await fetch('api/hidden', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ hide, unhide }),
      });
      if (r.ok) hidden = (await r.json()).hidden;
    } catch { /* offline: keep what we have */ }
  };
  // Sends any newly read posts and gets back the full shared list.
  const syncRead = async (add = []) => {
    try {
      const r = await fetch('api/read', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ add }),
      });
      if (r.ok) read = (await r.json()).readIds;
    } catch { /* offline: keep what we have */ }
  };
  // The last data seen is kept on this device, so pages draw instantly and then refresh in the background.
  const CACHE = 'jafWebCache';
  const saveCache = () => {
    try { localStorage.setItem(CACHE, JSON.stringify({ data, read, hidden })); } catch { /* storage full or blocked */ }
  };
  const load = async () => {
    const getData = (async () => {
      try {
        const r = await fetch('data.json', { cache: 'no-cache' });
        if (r.ok) {
          data = await r.json();
          dataTag = r.headers.get('ETag') || String(data.lastSync);
        }
      } catch { /* keep the last data we had */ }
    })();
    await Promise.all([getData, syncRead(), syncHidden()]);
    saveCache();
  };
  let cached = null;
  try { cached = JSON.parse(localStorage.getItem(CACHE) || 'null'); } catch { /* no cache */ }
  let ready;
  if (cached?.data) {
    ({ data, read = [], hidden = [] } = cached);
    ready = Promise.resolve();
    setTimeout(() => refresh(), 0); // fetch fresh data now, redraw if anything changed
  } else {
    ready = load();
  }
  const notify = () => listeners.forEach(fn => fn({}));

  document.documentElement.classList.add('web');

  window.chrome = {
    storage: {
      local: {
        async get(keys) {
          await ready;
          const all = { ...data, readIds: readIds(), hiddenCompanies: hidden, syncing };
          if (keys == null) return all;
          const list = typeof keys === 'string' ? [keys] : Array.isArray(keys) ? keys : Object.keys(keys);
          return Object.fromEntries(list.filter(k => k in all).map(k => [k, all[k]]));
        },
        async set(obj) {
          if (obj.readIds) {
            const have = new Set(read);
            read = [...new Set([...read, ...obj.readIds])]; // show it as read right away
            notify();
            await syncRead(obj.readIds.filter(id => !have.has(id)));
          }
          if (obj.hiddenCompanies) {
            const before = new Set(hidden);
            const after = new Set(obj.hiddenCompanies);
            hidden = obj.hiddenCompanies; // show the change right away
            notify();
            await syncHidden([...after].filter(c => !before.has(c)), [...before].filter(c => !after.has(c)));
          }
          notify();
        },
      },
      onChanged: { addListener: fn => listeners.push(fn) },
    },
    runtime: {
      async sendMessage(msg) {
        if (msg?.type === 'login') window.open(PORTAL, '_blank', 'noopener');
        if (msg?.type === 'sync') return syncNow();
        return { ok: false, msg: 'Only available in the extension' };
      },
    },
  };

  // "Sync now": asks the server to sync with the portal and blog, then waits for it to finish (up to 5 min).
  // onlyIfStale: when the site is opened, sync only if the last sync is over 5 minutes old.
  const syncNow = async (onlyIfStale = false) => {
    if (syncing) return { ok: true };
    if (onlyIfStale) {
      const r = await fetch('api/sync?stale=1', { method: 'POST' }).catch(() => null);
      const s = r?.ok ? await r.json() : null;
      if (!s?.started) return { ok: true };
    }
    syncing = true;
    notify();
    try {
      const r = onlyIfStale ? { ok: true } : await fetch('api/sync', { method: 'POST' });
      if (r.ok) {
        for (let i = 0; i < 100; i++) {
          await new Promise(res => setTimeout(res, 3000));
          const s = await fetch('api/sync', { cache: 'no-store' }).then(x => x.json()).catch(() => ({ running: true }));
          if (!s.running) break;
        }
      }
    } catch { /* offline */ }
    syncing = false;
    await load();
    notify();
    return { ok: true };
  };

  // Pick up new syncs and posts read on your other devices.
  const stamp = () => `${dataTag}|${data.lastSync}|${read.join()}|${hidden.join()}`;
  const refresh = async () => {
    const before = stamp();
    await load();
    if (stamp() !== before) notify();
  };

  // Push: the server says "update" the moment new data lands; the page reloads its data and redraws.
  // The browser reconnects by itself; phones drop the stream in the background, so it's reopened on return.
  let events = null;
  const listen = () => {
    if (events && events.readyState !== EventSource.CLOSED) return;
    events = new EventSource('api/events');
    events.onmessage = () => { if (!syncing) refresh(); };
  };
  listen();
  syncNow(true); // opening the site: sync if the last sync is over 5 minutes old
  setInterval(refresh, 120000); // fallback, in case the push stream is down
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    listen();
    refresh();
    syncNow(true);
  });
})();
