import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Docker deployment (deploy/docker-compose.prod.yml's admin-web service):
  // traces only the files each route actually needs into .next/standalone,
  // so the runtime image doesn't need node_modules installed at all. See
  // apps/admin-web/Dockerfile.
  output: "standalone",
  experimental: {
    serverActions: {
      // Backend's ingestion upload cap is 10 MiB (config.py's
      // `ingestion_max_upload_bytes`). Next's default server-action body
      // limit is ~1 MB, which would reject a realistically-sized knowledge
      // doc before the server action even ran. Raised to 12 MB to leave
      // headroom for multipart boundary/part-header overhead on top of the
      // 10 MiB payload (S13.3 decision 3).
      bodySizeLimit: "12mb",
    },
  },
};

export default nextConfig;
