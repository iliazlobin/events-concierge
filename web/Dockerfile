# syntax=docker/dockerfile:1.7

ARG NODE_IMAGE=node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2

FROM ${NODE_IMAGE} AS dependencies
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json package-lock.json ./
RUN npm ci

FROM ${NODE_IMAGE} AS builder
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY --from=dependencies /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM ${NODE_IMAGE} AS runtime
ENV HOSTNAME=0.0.0.0 \
    NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000
WORKDIR /app
RUN addgroup --system --gid 10001 events-concierge \
    && adduser --system --uid 10001 --ingroup events-concierge nextjs
COPY --from=builder --chown=nextjs:events-concierge /app/.next/standalone ./
COPY --from=builder --chown=nextjs:events-concierge /app/.next/static ./.next/static
USER nextjs
EXPOSE 3000
STOPSIGNAL SIGTERM
CMD ["node", "server.js"]
