FROM node:24-bookworm-slim
WORKDIR /app
COPY autotrader/package.json autotrader/package-lock.json ./autotrader/
RUN npm ci --prefix autotrader --omit=dev --ignore-scripts && mkdir /state && chown node:node /state
COPY autotrader/engine.mjs autotrader/store.mjs autotrader/toss.mjs autotrader/research.mjs autotrader/demo.mjs autotrader/cli.mjs ./autotrader/
USER node
ENTRYPOINT ["node", "autotrader/cli.mjs"]
CMD ["help"]
