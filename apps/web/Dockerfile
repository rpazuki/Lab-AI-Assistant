FROM node:22-slim AS deps
WORKDIR /app/apps/web
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm ci --omit=dev || npm install --omit=dev

FROM node:22-slim AS build
WORKDIR /app/apps/web
ARG NEXT_PUBLIC_BASE_PATH=/assist
ENV NEXT_PUBLIC_BASE_PATH=$NEXT_PUBLIC_BASE_PATH
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm ci || npm install
COPY apps/web ./
RUN npm run build

FROM node:22-slim AS runtime
ENV NODE_ENV=production
WORKDIR /app/apps/web
COPY --from=deps /app/apps/web/node_modules ./node_modules
COPY --from=build /app/apps/web/.next ./.next
COPY --from=build /app/apps/web/package.json ./package.json
COPY --from=build /app/apps/web/next.config.mjs ./next.config.mjs
COPY --from=build /app/apps/web/public ./public
USER node
EXPOSE 3002
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=10 \
  CMD node -e "fetch('http://127.0.0.1:3002' + (process.env.APP_BASE_PATH || '/assist') + '/login').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"
CMD ["npx", "next", "start", "-p", "3002"]
