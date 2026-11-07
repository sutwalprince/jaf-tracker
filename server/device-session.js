// Passkey login, plus a device key that keeps this browser logged in. The device key can't be
// read by scripts, so it can't be exported the way a cookie can. It renews the short login
// cookie silently, and a cookie copied elsewhere stops working when it expires.
(() => {
  // Filled in by web.py from ~/jaf-sync/site.json (kept out of git).
  const USER = "__JAF_USER__";                // the name passkeys are saved under
  const PARENT_ORIGIN = "__JAF_PARENT_ORIGIN__"; // a page of yours that keeps the login fresh (optional)
  const BASE = '/passkey';
  const KEEP_ALIVE_MS = 5 * 60 * 1000;
  const CEREMONY_MS = 60 * 1000;
  const keepAlive = 'keepAlive' in (document.currentScript?.dataset ?? {});

  const encode = (buffer) =>
    btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const decode = (text) => Uint8Array.from(atob(text.replace(/-/g, '+').replace(/_/g, '/')), (c) => c.charCodeAt(0));

  const post = (path, body = {}) =>
    fetch(BASE + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

  function withStore(mode, action) {
    return new Promise((resolve, reject) => {
      const open = indexedDB.open('device-session', 1);
      open.onupgradeneeded = () => open.result.createObjectStore('keys');
      open.onerror = () => reject(open.error);
      open.onsuccess = () => {
        const transaction = open.result.transaction('keys', mode);
        const request = action(transaction.objectStore('keys'));
        transaction.oncomplete = () => {
          open.result.close();
          resolve(request.result);
        };
        transaction.onerror = () => reject(transaction.error);
      };
    });
  }

  // "false" is what makes the private key impossible for any script to export.
  const newDeviceKey = () => crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign']);
  const publicPart = async (pair) => encode(await crypto.subtle.exportKey('spki', pair.publicKey));

  async function saveDevice(response, pair) {
    const { deviceId } = await response.json();
    await withStore('readwrite', (store) => store.put({ deviceId, privateKey: pair.privateKey }, 'current'));
    navigator.storage?.persist?.().catch(() => {});
  }

  let refreshing = null;

  // Resolves to true if this browser now has a fresh cookie. It never prompts the person.
  function refresh() {
    refreshing ??= (async () => {
      try {
        const device = await withStore('readonly', (store) => store.get('current'));
        if (!device) return false;
        const { challenge } = await (await post('/challenge')).json();
        const signature = await crypto.subtle.sign(
          { name: 'ECDSA', hash: 'SHA-256' },
          device.privateKey,
          new TextEncoder().encode(challenge),
        );
        const response = await post('/refresh', { deviceId: device.deviceId, challenge, signature: encode(signature) });
        return response.ok;
      } catch {
        return false;
      } finally {
        refreshing = null;
      }
    })();
    return refreshing;
  }

  async function login() {
    const { challenge, rpId } = await (await post('/challenge')).json();
    const credential = await navigator.credentials.get({
      publicKey: { challenge: decode(challenge), rpId, userVerification: 'required', timeout: CEREMONY_MS },
    });
    const pair = await newDeviceKey();
    const response = await post('/login', {
      credentialId: credential.id,
      clientData: encode(credential.response.clientDataJSON),
      authenticatorData: encode(credential.response.authenticatorData),
      signature: encode(credential.response.signature),
      deviceKey: await publicPart(pair),
    });
    if (response.ok) await saveDevice(response, pair);
    return response.status;
  }

  // Returns 401 for a wrong password and 429 when locked out, before any browser prompt.
  async function addDevice(password) {
    const options = await post('/register-options', { password });
    if (!options.ok) return options.status;

    const { challenge, rpId, existing } = await options.json();
    const credential = await navigator.credentials.create({
      publicKey: {
        rp: { id: rpId, name: document.title.split('|').pop().trim() },
        user: { id: new TextEncoder().encode(USER), name: USER, displayName: USER },
        challenge: decode(challenge),
        pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
        authenticatorSelection: { residentKey: 'required', userVerification: 'required' },
        excludeCredentials: existing.map((id) => ({ type: 'public-key', id: decode(id) })),
        attestation: 'none',
        timeout: CEREMONY_MS,
      },
    });
    const pair = await newDeviceKey();
    const response = await post('/register', {
      credentialId: credential.id,
      clientData: encode(credential.response.clientDataJSON),
      authenticatorData: encode(credential.response.getAuthenticatorData()),
      publicKey: encode(credential.response.getPublicKey()),
      deviceKey: await publicPart(pair),
    });
    if (response.ok) await saveDevice(response, pair);
    return response.status;
  }

  window.deviceSession = { refresh, login, addDevice, supported: Boolean(window.PublicKeyCredential) };

  if (keepAlive) {
    // Inside the catalog's hidden frame, tell the catalog once the login is fresh.
    refresh().then((renewed) => {
      if (renewed && window.parent !== window && PARENT_ORIGIN) window.parent.postMessage('session-refreshed', PARENT_ORIGIN);
    });
    setInterval(() => {
      if (!document.hidden) refresh();
    }, KEEP_ALIVE_MS);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) refresh();
    });
  }
})();
