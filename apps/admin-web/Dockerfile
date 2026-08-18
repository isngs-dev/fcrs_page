# Multi-stage build for admin-web (CLAUDE.md packaging standard: "Docker
# multi-stage, non-root, healthchecks"). Next.js `output: "standalone"`
# (next.config.ts) traces only the files each route actually needs into
# .next/standalone, so the runtime image needs no node_modules install --
# only the traced output + public/ + .next/static (the standalone server.js
# does not serve those two on its own; see Next's self-hosting guide).
#
# Build context is this directory (apps/admin-web has no sibling-package
# dependency, unlike services/api/Dockerfile):
#
#   docker build -f apps/admin-web/Dockerfile -t chatbot-admin-web:<tag> apps/admin-web

FROM node:24-slim AS builder
WORKDIR /build

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .
# lib/env.ts validates ADMIN_API_BASE_URL/JWT_SECRET EAGERLY at module-import
# time (fail-fast, by design) -- and `next build`'s page-data-collection
# step actually imports route modules for static analysis, so the build
# itself fails without SOME value present, even though these are
# server-only and never inlined into client JS. Build-time placeholders
# only: `node server.js` (the runtime CMD below) re-evaluates lib/env.ts
# fresh in a separate process when the container actually starts, reading
# whatever real values docker-compose's env_file/environment supply then --
# see next.config.ts's "Environment Variables" doc note on why this is safe
# (a single image promoted through environments with different runtime env).
ENV ADMIN_API_BASE_URL=http://build-time-placeholder:8000
ENV JWT_SECRET=build-time-placeholder-value-replaced-at-container-startup
RUN npm run build

FROM node:24-slim AS runtime

RUN groupadd --system app && useradd --system --gid app --create-home --home-dir /home/app app

WORKDIR /app
COPY --from=builder /build/.next/standalone ./
COPY --from=builder /build/public ./public
COPY --from=builder /build/.next/static ./.next/static

RUN chown -R app:app /app
USER app

ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0

EXPOSE 3000

# /login is the one route excluded from proxy.ts's auth matcher (real
# middleware, not just client-side gating -- see AGENTS.md note that this
# Next.js version renamed middleware.ts to proxy.ts), so it is reachable
# with no cookie and no backend call -- a clean liveness probe. Node has no
# curl in slim; use the stdlib instead, same trick as services/api/Dockerfile.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD node -e "require('http').get('http://localhost:3000/login', r => process.exit(r.statusCode === 200 ? 0 : 1)).on('error', () => process.exit(1))"

CMD ["node", "server.js"]
