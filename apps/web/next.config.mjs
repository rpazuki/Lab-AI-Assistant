const basePath = process.env.NEXT_PUBLIC_BASE_PATH || "/assist";

/**
 * `next dev` builds its modules and its hot reload with `eval`, so under a policy
 * without 'unsafe-eval' a dev page renders but never runs a script: buttons do
 * nothing and forms submit as plain HTML. Development alone gets it; a build
 * (`next build`, which is what every deployment runs) never does.
 */
const scriptSrc =
  process.env.NODE_ENV === "development"
    ? "script-src 'self' 'unsafe-inline' 'unsafe-eval'"
    : "script-src 'self' 'unsafe-inline'";

/**
 * The browser never talks to the API directly and never holds a bearer token.
 * Requests go to same-origin routes under /api, which attach the httpOnly
 * session cookie server-side — the pattern the predecessor arrived at, kept.
 */
/** @type {import("next").NextConfig} */
const nextConfig = {
  basePath,
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
              `${scriptSrc}; connect-src 'self'; frame-ancestors 'none'`,
          },
        ],
      },
    ];
  },
};

export default nextConfig;
