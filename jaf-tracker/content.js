// Runs on the placement portal. Hands the portal's login token to the extension
// so it can read your applications. It never clicks, submits or changes anything.
let last = null;

function check() {
  let token = null;
  try { token = localStorage.getItem('token'); } catch { return; }
  if (token && token !== last) {
    last = token;
    chrome.runtime.sendMessage({ type: 'token', token }).catch(() => {});
  }
}

check();
// The portal is a single-page app, so a fresh login doesn't reload the page.
setInterval(check, 5000);
