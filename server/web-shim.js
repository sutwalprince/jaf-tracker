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
  let offline = navigator.onLine === false; // true when the last try to reach the server failed
  // Changes made while the server can't be reached; sent as soon as it can.
  let pendingRead = [];
  let pendingHide = [];
  let pendingUnhide = [];

  const TIMEOUT = 20000; // a server that doesn't answer counts as offline after this long
  const call = async (url, init = {}) => {
    const send = () => fetch(url, { signal: AbortSignal.timeout(TIMEOUT), ...init });
    let response = await send();
    // The short login cookie ran out, so renew it with this device's key and try again.
    if (response.status === 401 && await window.deviceSession?.refresh()) response = await send();
    if (response.status === 401) {
      // The login has ended, so remove the data saved on this device and ask again.
      try { localStorage.removeItem('jafWebCache'); } catch { /* storage blocked */ }
      location.replace('/login');
    }
    return response;
  };
  const post = (url, body) => call(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const readIds = () => read;
  // Hidden companies, shared by all your devices.
  const syncHidden = async (hide = [], unhide = []) => {
    pendingHide = [...new Set([...pendingHide.filter(c => !unhide.includes(c)), ...hide])];
    pendingUnhide = [...new Set([...pendingUnhide.filter(c => !hide.includes(c)), ...unhide])];
    const sentHide = pendingHide;
    const sentUnhide = pendingUnhide;
    try {
      const r = await post('api/hidden', { hide: sentHide, unhide: sentUnhide });
      if (r.ok) {
        const server = (await r.json()).hidden;
        pendingHide = pendingHide.filter(c => !sentHide.includes(c));
        pendingUnhide = pendingUnhide.filter(c => !sentUnhide.includes(c));
        hidden = [...new Set([...server, ...pendingHide])].filter(c => !pendingUnhide.includes(c));
      }
    } catch { /* offline: stays pending */ }
  };
  // Sends any newly read posts and gets back the full shared list.
  const syncRead = async (add = []) => {
    pendingRead = [...new Set([...pendingRead, ...add])];
    const sent = pendingRead;
    try {
      const r = await post('api/read', { add: sent });
      if (r.ok) {
        const server = (await r.json()).readIds;
        pendingRead = pendingRead.filter(id => !sent.includes(id));
        read = [...new Set([...server, ...pendingRead])];
      }
    } catch { /* offline: stays pending */ }
  };
  // The last data seen is kept on this device, so pages draw instantly and then refresh in the background.
  const CACHE = 'jafWebCache';
  const saveCache = () => {
    try {
      localStorage.setItem(CACHE, JSON.stringify({ data, read, hidden, pendingRead, pendingHide, pendingUnhide }));
    } catch { /* storage full or blocked */ }
  };
  const load = async () => {
    const getData = (async () => {
      try {
        const r = await call('data.json', { cache: 'no-cache' });
        offline = !r.ok;
        if (r.ok) {
          data = await r.json();
          dataTag = r.headers.get('ETag') || String(data.lastSync);
        }
      } catch { offline = true; /* keep the last data we had */ }
    })();
    await Promise.all([getData, syncRead(), syncHidden()]);
    saveCache();
  };
  let cached = null;
  try { cached = JSON.parse(localStorage.getItem(CACHE) || 'null'); } catch { /* no cache */ }
  let ready;
  if (cached?.data) {
    ({ data, read = [], hidden = [], pendingRead = [], pendingHide = [], pendingUnhide = [] } = cached);
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
          const all = { ...data, readIds: readIds(), hiddenCompanies: hidden, syncing, offline };
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
            saveCache();
          }
          if (obj.hiddenCompanies) {
            const before = new Set(hidden);
            const after = new Set(obj.hiddenCompanies);
            hidden = obj.hiddenCompanies; // show the change right away
            notify();
            await syncHidden([...after].filter(c => !before.has(c)), [...before].filter(c => !after.has(c)));
            saveCache();
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
      const r = await call('api/sync?stale=1', { method: 'POST' }).catch(() => null);
      const s = r?.ok ? await r.json() : null;
      if (!s?.started) return { ok: true };
    }
    syncing = true;
    notify();
    try {
      const r = onlyIfStale ? { ok: true } : await call('api/sync', { method: 'POST' });
      if (r.ok) {
        for (let i = 0; i < 100; i++) {
          await new Promise(res => setTimeout(res, 3000));
          // Lost the server mid-sync: stop waiting, load() below reports it as offline.
          const s = await call('api/sync', { cache: 'no-store' }).then(x => x.json()).catch(() => ({ running: false }));
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
  const stamp = () => `${dataTag}|${data.lastSync}|${read.join()}|${hidden.join()}|${offline}`;
  const refresh = async () => {
    const before = stamp();
    const wasOffline = offline;
    await load();
    if (stamp() !== before) notify();
    if (wasOffline && !offline) syncNow(true); // back online: catch up if the data is old
  };

  // Push: the server says "update" the moment new data lands; the page reloads its data and redraws.
  // The browser reconnects by itself; phones drop the stream in the background, so it's reopened on return.
  let events = null;
  const listen = () => {
    if (events && events.readyState !== EventSource.CLOSED) return;
    events = new EventSource('api/events');
    events.onmessage = () => { if (!syncing) refresh(); };
    events.onopen = () => { if (offline) refresh(); }; // the server is reachable again
  };
  listen();
  syncNow(true); // opening the site: sync if the last sync is over 5 minutes old
  setInterval(refresh, 120000); // fallback, in case the push stream is down
  // While offline, keep checking for the server: Tailscale coming back doesn't fire any browser event.
  setInterval(() => { if (offline && !document.hidden) { listen(); refresh(); } }, TIMEOUT + 5000);
  window.addEventListener('online', refresh);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    listen();
    refresh();
    syncNow(true);
    swRegistration?.update().catch(() => {}); // pick up a new version of the site
  });

  // Keeps the site's files on this device, so it opens instantly and works offline (see sw.js).
  // When a new version has installed itself, reload once to use it.
  let swRegistration = null;
  if ('serviceWorker' in navigator) {
    const hadVersion = !!navigator.serviceWorker.controller;
    let reloading = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (!hadVersion || reloading) return;
      reloading = true;
      location.reload();
    });
    navigator.serviceWorker.register('sw.js').then(r => { swRegistration = r; }).catch(() => {});
  }
})();
