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
COPY --from=build /app/apps/web/public ./public
USER node
EXPOSE 3002
CMD ["npx", "next", "start", "-p", "3002"]
