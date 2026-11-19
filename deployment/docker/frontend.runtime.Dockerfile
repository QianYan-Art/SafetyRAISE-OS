# 默认使用固定摘要的官方 Nginx 镜像。离线时可传入已核实的本地官方基础镜像标签。
# 旧版 docker-compose 使用单一宿主 build context，目录内仅放本文件、配置和 dist。
ARG RUNTIME_BASE_IMAGE=nginx:1.30.7-alpine@sha256:bef154735288bc38d99fac41ae63730684bcc661503673a04a0f9bc526ba8598
FROM ${RUNTIME_BASE_IMAGE}

WORKDIR /usr/share/nginx/html

# 删除运行镜像自带页面，避免旧静态资源残留后再复制本次预构建产物。
RUN rm -rf /usr/share/nginx/html/*

# 预构建 context 由发布准备步骤提供，不触发 Node 依赖下载。
COPY dist /usr/share/nginx/html
COPY nginx.frontend.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
