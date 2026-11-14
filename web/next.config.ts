import type { NextConfig } from "next";

// Static export: the dashboard is plain files on S3 behind CloudFront + Cognito (Lambda@Edge).
// All data is fetched at runtime from /data/*.json, so new runs and studies need no rebuild.
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
  turbopack: {
    root: __dirname,
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
