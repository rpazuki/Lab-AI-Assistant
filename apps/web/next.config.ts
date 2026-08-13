import type { NextConfig } from "next";

/**
 * The browser never talks to the API directly and never holds a bearer token.
 * Requests go to same-origin routes under /api, which attach the httpOnly
 * session cookie server-side — the pattern the predecessor arrived at, kept.
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "same-origin" },
          {
            key: "Content-Security-Policy",
            value:
              "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; " +
              "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
