FROM node:22.14.0-alpine AS base
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1

FROM base AS deps
COPY package.json package-lock.json ./
RUN npm ci

FROM deps AS test
COPY . .
RUN npm run lint && npm test

FROM deps AS builder
COPY . .
ARG DEVFLOW_API_URL=http://127.0.0.1:8000
ENV DEVFLOW_API_URL=${DEVFLOW_API_URL}
RUN npm run build

FROM base AS production
ENV NODE_ENV=production
RUN addgroup --system --gid 10001 devflow \
    && adduser --system --uid 10001 --ingroup devflow devflow
COPY --from=builder --chown=devflow:devflow /app/.next/standalone ./
COPY --from=builder --chown=devflow:devflow /app/.next/static ./.next/static
USER 10001:10001
EXPOSE 3000
CMD ["node", "server.js"]
