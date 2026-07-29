# App shell (Shopify React Router template) — multi-stage image.
#
# The upstream template's single stage did `npm ci --omit=dev` and then `npm run build`.
# That cannot work in this repo: `react-router build` loads @react-router/dev, which loads
# vite, and vite + typescript are devDependencies — so the build step ran against a tree
# that had them pruned. Splitting the stages is the cleaner fix than reclassifying
# build-time packages as runtime dependencies: it keeps package.json honest about what is
# actually needed to serve, and keeps the toolchain out of the shipped image entirely.

# ---------- build ----------
FROM node:20-alpine AS build

# Prisma's query engine is linked against OpenSSL.
RUN apk add --no-cache openssl

WORKDIR /app

COPY package.json package-lock.json* ./

# `npm ci` needs a lockfile, and app/.gitignore deliberately ignores package-lock.json
# (the Shopify template ships none — CI uses `npm install` for the same reason). A local
# build has the lockfile on disk and gets the reproducible path; a build from a clean
# checkout falls back. Closing that gap is a provisioning decision, not a Dockerfile one.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY . .

# The server bundle imports @prisma/client, so the client has to exist before the build.
RUN npx prisma generate

RUN npm run build

# ---------- runtime ----------
FROM node:20-alpine AS runtime

RUN apk add --no-cache openssl

ENV NODE_ENV=production

WORKDIR /app

COPY package.json package-lock.json* ./

# prisma/ is copied before the install so @prisma/client's postinstall can generate
# against the real schema. It is also needed at container start: `docker-start` runs
# `prisma generate && prisma migrate deploy` (never `migrate dev` — that can reset the DB).
COPY prisma ./prisma

RUN if [ -f package-lock.json ]; then npm ci --omit=dev; else npm install --omit=dev; fi \
    && npm cache clean --force

COPY --from=build /app/build ./build

EXPOSE 3000

CMD ["npm", "run", "docker-start"]
