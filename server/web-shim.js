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
  const load = async () => {
    try {
      const r = await fetch('data.json', { cache: 'no-store' });
      if (r.ok) data = await r.json();
    } catch { /* keep the last data we had */ }
    await Promise.all([syncRead(), syncHidden()]);
  };
  const ready = load();
  const notify = () => listeners.forEach(fn => fn({}));

  document.documentElement.classList.add('web');

  window.chrome = {
    storage: {
      local: {
        async get(keys) {
          await ready;
          const all = { ...data, readIds: readIds(), hiddenCompanies: hidden };
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
        return { ok: false, msg: 'Only available in the extension' };
      },
    },
  };

  // Pick up new syncs and posts read on your other devices.
  const refresh = async () => {
    const before = `${data.lastSync}|${read.length}|${hidden.join()}`;
    await load();
    if (`${data.lastSync}|${read.length}|${hidden.join()}` !== before) notify();
  };
  setInterval(refresh, 60000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
})();
