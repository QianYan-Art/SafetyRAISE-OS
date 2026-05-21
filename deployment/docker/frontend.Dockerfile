FROM node:20-alpine AS build

ARG VITE_API_BASE=

WORKDIR /app/frontend

COPY frontend/package.json /app/frontend/package.json
COPY frontend/package-lock.json /app/frontend/package-lock.json

RUN npm ci

COPY frontend /app/frontend

ENV VITE_API_BASE=${VITE_API_BASE}

RUN npm run build

FROM nginx:1.27-alpine

COPY deployment/docker/nginx.frontend.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/frontend/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
