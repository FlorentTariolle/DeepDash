export default {
  async fetch(request, env) {
    if (!env.ASSETS || typeof env.ASSETS.fetch !== "function") {
      return new Response("Static asset binding unavailable", { status: 503 });
    }
    return env.ASSETS.fetch(request);
  },
};
