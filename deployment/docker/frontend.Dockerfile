FROM node:22-alpine AS build

ARG VITE_API_BASE=

WORKDIR /app/frontend

COPY frontend/package.json /app/frontend/package.json
COPY frontend/package-lock.json /app/frontend/package-lock.json

RUN npm ci

COPY frontend /app/frontend

ENV VITE_API_BASE=${VITE_API_BASE}

RUN npm run build

FROM nginx:1.30.7-alpine@sha256:bef154735288bc38d99fac41ae63730684bcc661503673a04a0f9bc526ba8598

COPY deployment/docker/nginx.frontend.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/frontend/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
