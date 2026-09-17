const backendUrl = process.env.DEVFLOW_API_URL || "http://127.0.0.1:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  // Hostnames only; keep unrelated dev origins blocked.
  allowedDevOrigins: ["127.0.0.1"],
  // The gzip rewrite proxy buffers SSE chunks until its compression window fills.
  // Keep durable events live; an upstream proxy must also avoid buffering SSE.
  compress: false,
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${backendUrl}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;

