/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Emit a self-contained server bundle (.next/standalone) for the slim Docker
  // runtime image — copies only the traced node_modules.
  output: "standalone",
  // Proxy API + WS to the FastAPI backend so the browser talks same-origin.
  async rewrites() {
    const backend = process.env.BACKEND_URL || "http://localhost:8000";
    return [
      { source: "/api/:path*", destination: `${backend}/:path*` },
    ];
  },
};
module.exports = nextConfig;
