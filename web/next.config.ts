import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  turbopack: {
    root: process.cwd(),
  },
  /**
   * Let a deploy actually reach the browser.
   *
   * The app's pages are prerendered and were served with `s-maxage` only, which
   * says nothing to a browser - so Chrome cached the document heuristically. The
   * script chunks are content-hashed and `immutable`, which is right, but it
   * means a stale document keeps asking for the *old* chunk names forever: the
   * new build is deployed and simply never fetched. Revalidating the document on
   * every load costs one conditional request and is what makes a plain reload
   * pick up a release.
   */
  async headers() {
    return [
      {
        // Everything except the content-hashed build output, which is immutable
        // by name and must keep its long-lived caching.
        source: "/((?!_next/static|_next/image).*)",
        headers: [
          { key: "Cache-Control", value: "no-cache, must-revalidate" },
        ],
      },
    ];
  },
};

export default nextConfig;
