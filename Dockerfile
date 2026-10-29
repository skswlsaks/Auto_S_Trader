FROM node:24-bookworm-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && mkdir /state && chown node:node /state
COPY engine.mjs store.mjs toss.mjs research.mjs demo.mjs cli.mjs ./
USER node
ENTRYPOINT ["node", "cli.mjs"]
CMD ["help"]
