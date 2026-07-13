# Multi-stage Next.js build → slim standalone runtime.
# Requires `output: "standalone"` in next.config.js.
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# The /api rewrite destination is resolved AT BUILD TIME into routes-manifest,
# so the backend URL must be known here — runtime env is too late in standalone.
ARG BACKEND_URL=http://backend:8000
# NEXT_PUBLIC_* vars are INLINED into the client bundle at build time too: a
# hosted deploy must pass the public WS origin here (e.g. api.yourdomain.com)
# or browsers will try ws://localhost:8000. Empty default = dev fallback.
ARG NEXT_PUBLIC_WS_HOST=""
ENV BACKEND_URL=$BACKEND_URL \
    NEXT_PUBLIC_WS_HOST=$NEXT_PUBLIC_WS_HOST \
    NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
# HOSTNAME=0.0.0.0: docker sets HOSTNAME to the container id, which makes the
# standalone server bind to that name only — localhost healthchecks then fail.
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
RUN addgroup -S nodejs && adduser -S nextjs -G nodejs

# `standalone` bundles a minimal server + only the node_modules it traced.
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
