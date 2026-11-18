// Cloudflare Worker — transparent reverse proxy for score.joinrabble.world → the sCoRE host
//
// All requests to score.joinrabble.world are forwarded verbatim to the sCoRE
// backend. Path, method, headers, and body are preserved. CORS headers come
// from the sCoRE server (FRONTEND_URL env var) and flow through unchanged.
// The backend origin is the Worker secret UPSTREAM (not in the repo):
//   npx wrangler secret put UPSTREAM      # e.g. https://<service>.example.com
// The scheduled handler is the keep-warm ping (cron in wrangler.jsonc).

export default {
  async fetch(request, env) {
    if (!env.UPSTREAM) return new Response('sCoRE upstream not configured', { status: 503 });
    const url = new URL(request.url);
    const upstream = new URL(env.UPSTREAM);
    url.hostname = upstream.hostname;
    url.protocol = upstream.protocol;
    url.port = upstream.port;
    return fetch(new Request(url.toString(), request));
  },

  async scheduled(event, env, ctx) {
    if (env.UPSTREAM) ctx.waitUntil(fetch(env.UPSTREAM + '/health').catch(() => {}));
  },
};
