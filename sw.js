/**
 * sw.js – Service Worker for Virtual Edge
 * ========================================
 * Handles:
 *   1. Web Push notification reception + display
 *   2. Notification click → focus or open the app
 *   3. Offline cache for core app shell assets
 */

const CACHE_NAME  = "virtual-edge-v1";
const CACHE_URLS  = [
  "./index.html",
  "./style.css",
  "./app.js",
  "./push.js",
  "./manifest.json",
];

// ──────────────────────────────────────────────────────────────────
// Install – cache app shell
// ──────────────────────────────────────────────────────────────────
self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(CACHE_URLS))
  );
  self.skipWaiting();
});

// ──────────────────────────────────────────────────────────────────
// Activate – clean old caches
// ──────────────────────────────────────────────────────────────────
self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// ──────────────────────────────────────────────────────────────────
// Fetch – serve from cache, fall back to network
// ──────────────────────────────────────────────────────────────────
self.addEventListener("fetch", event => {
  // Only cache GET requests for same origin
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== location.origin) return;

  event.respondWith(
    caches.match(event.request).then(cached => {
      if (cached) return cached;
      return fetch(event.request).then(response => {
        // Cache fresh copies of app shell files
        if (response.ok && CACHE_URLS.some(u => event.request.url.endsWith(u.replace("./", "")))) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => caches.match("./index.html"));
    })
  );
});

// ──────────────────────────────────────────────────────────────────
// Push – receive and show notification
// ──────────────────────────────────────────────────────────────────
self.addEventListener("push", event => {
  let data = {
    title: "🔥 Virtual Edge",
    body:  "New prediction available!",
    url:   "/",
    icon:  "./icon-192.png",
    badge: "./icon-192.png",
  };

  if (event.data) {
    try {
      const parsed = event.data.json();
      data = { ...data, ...parsed };
    } catch (e) {
      data.body = event.data.text();
    }
  }

  const options = {
    body:    data.body,
    icon:    data.icon,
    badge:   data.badge,
    tag:     "ve-prediction",          // replaces previous notification
    renotify: true,
    requireInteraction: false,
    vibrate: [200, 100, 200],
    data:    { url: data.url },
    actions: [
      { action: "view",    title: "View Predictions" },
      { action: "dismiss", title: "Dismiss"          },
    ],
  };

  event.waitUntil(
    self.registration.showNotification(data.title, options)
  );

  // Also post message to all open app tabs so they can update live
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true })
      .then(clients =>
        clients.forEach(client =>
          client.postMessage({ type: "push_received", data })
        )
      )
  );
});

// ──────────────────────────────────────────────────────────────────
// Notification click – open or focus the app
// ──────────────────────────────────────────────────────────────────
self.addEventListener("notificationclick", event => {
  event.notification.close();

  if (event.action === "dismiss") return;

  const targetUrl = event.notification.data?.url || "/";

  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true })
      .then(clients => {
        // Focus existing tab if open
        const existing = clients.find(c => c.url.includes(targetUrl) && "focus" in c);
        if (existing) return existing.focus();
        // Otherwise open a new tab
        if (self.clients.openWindow) return self.clients.openWindow(targetUrl);
      })
  );
});

// ──────────────────────────────────────────────────────────────────
// Push subscription change – re-subscribe automatically
// ──────────────────────────────────────────────────────────────────
self.addEventListener("pushsubscriptionchange", event => {
  event.waitUntil(
    self.registration.pushManager.subscribe({
      userVisibleOnly:      true,
      applicationServerKey: event.oldSubscription?.options?.applicationServerKey,
    }).then(sub =>
      fetch("/api/push/subscribe", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({
          endpoint: sub.endpoint,
          keys: {
            p256dh: btoa(String.fromCharCode(...new Uint8Array(sub.getKey("p256dh")))),
            auth:   btoa(String.fromCharCode(...new Uint8Array(sub.getKey("auth")))),
          },
        }),
      })
    )
  );
});
