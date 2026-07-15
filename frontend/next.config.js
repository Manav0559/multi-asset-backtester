/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Emit a self-contained server bundle (.next/standalone) for the slim Docker
  // runtime image — copies only the traced node_modules.
  output: "standalone",
  // Proxy the API to the FastAPI backend so the browser talks same-origin.
  async rewrites() {
    // BACKEND_URL is host-supplied and has been fat-fingered before (a pasted
    // markdown link left a stray "]" in the value), which makes Next fail to
    // parse this rewrite at build time. Defensively extract the first well-formed
    // origin and trim any trailing slash so a messy value can't break the build.
    const raw = process.env.BACKEND_URL || "http://localhost:8000";
    const backend = (raw.match(/https?:\/\/[^\s\])]+/)?.[0] || "http://localhost:8000").replace(/\/+$/, "");
    return [
      { source: "/api/:path*", destination: `${backend}/:path*` },
    ];
  },
};
module.exports = nextConfig;
