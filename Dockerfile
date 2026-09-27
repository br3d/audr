# Optional container image. Render's native Node runtime (see render.yaml) does
# not need this, but it keeps us portable to Railway / Fly.io / any host that
# takes a Dockerfile — no lock-in.
FROM node:20-slim

ENV NODE_ENV=production
WORKDIR /app

# Install prod deps first for layer caching.
COPY package.json package-lock.json* ./
RUN npm ci --omit=dev

COPY . .

# Non-root runtime user.
USER node

EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||3000)+'/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["npm", "start"]
