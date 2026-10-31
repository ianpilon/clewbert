/* The installed app's service worker: it shows push notifications (see push.py) and opens the right task when one
   is tapped. It caches nothing: every page and API call still goes to the server, as in the browser. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', e => {
  let m = {};
  try { m = e.data ? e.data.json() : {}; } catch (err) {}
  e.waitUntil(self.registration.showNotification(m.title || 'Clewbert', {
    body: m.body || '', tag: m.tag || undefined, renotify: !!m.tag, data: {url: m.url || './#agents'},
    icon: '/icons/bulb-192.png', badge: '/icons/badge-96.png'}));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = new URL(e.notification.data && e.notification.data.url || './#agents', self.registration.scope).href;
  e.waitUntil((async () => {
    const open = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    const win = open.find(c => c.url.startsWith(self.registration.scope));
    if (win) { await win.focus(); return win.navigate(url).catch(() => win.postMessage({go: url})); }
    return self.clients.openWindow(url);
  })());
});
