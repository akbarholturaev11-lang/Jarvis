"use strict";

/*
 * Public shell only. API, session, evidence, and every other dynamic response
 * remain network-owned and are never intercepted or cached by this worker.
 */
const OWNED_CACHE_PREFIX = "jarvis-admin-shell-";
const CACHE_NAME = `${OWNED_CACHE_PREFIX}v1`;
const SCOPE_URL = new URL(self.registration.scope);
const SHELL_URLS = [
  new URL("./", SCOPE_URL).href,
  new URL("index.html", SCOPE_URL).href,
  new URL("styles.css", SCOPE_URL).href,
  new URL("app.js", SCOPE_URL).href,
  new URL("i18n.json", SCOPE_URL).href,
  new URL("manifest.webmanifest", SCOPE_URL).href,
  new URL("icons/admin-icon.svg", SCOPE_URL).href,
];
const SHELL_PATHS = new Set(SHELL_URLS.map((value) => new URL(value).pathname));

self.addEventListener("install", (event) => {
  const requests = SHELL_URLS.map(
    (url) => new Request(url, { cache: "reload", credentials: "same-origin" }),
  );
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(requests)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys
        .filter((key) => key.startsWith(OWNED_CACHE_PREFIX) && key !== CACHE_NAME)
        .map((key) => caches.delete(key)),
    )),
  );
  self.clients.claim();
});

async function publicShellResponse(request) {
  try {
    const response = await fetch(request);
    if (response.ok && response.type === "basic") {
      const cache = await caches.open(CACHE_NAME);
      await cache.put(request, response.clone());
    }
    return response;
  } catch (_error) {
    const cached = await caches.match(request, { ignoreSearch: false });
    if (cached) return cached;
    if (request.mode === "navigate") {
      const shell = await caches.match(new URL("./", SCOPE_URL).href);
      if (shell) return shell;
    }
    return Response.error();
  }
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);

  if (url.origin !== SCOPE_URL.origin) return;
  if (url.pathname.startsWith("/api/")) return;
  if (url.search || url.hash || !SHELL_PATHS.has(url.pathname)) return;

  event.respondWith(publicShellResponse(request));
});
