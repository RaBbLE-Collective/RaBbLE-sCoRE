// Cloudflare Worker — transparent reverse proxy for score.joinrabble.world → Render
//
// All requests to score.joinrabble.world are forwarded verbatim to the Render
// backend. Path, method, headers, and body are preserved. CORS headers come
// from the Render sCoRE server (FRONTEND_URL env var) and flow through
// unchanged. Swapping the Render URL only requires redeploying this Worker.
// The scheduled handler is the keep-warm ping (cron in wrangler.jsonc).

const UPSTREAM = 'https://score-origin.invalid';

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const upstream = new URL(UPSTREAM);
    url.hostname = upstream.hostname;
    url.protocol = upstream.protocol;
    url.port = upstream.port;
    return fetch(new Request(url.toString(), request));
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(fetch(UPSTREAM + '/health').catch(() => {}));
  },
};
