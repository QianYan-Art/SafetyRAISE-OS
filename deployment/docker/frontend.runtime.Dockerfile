# 仅复用已核实的本地 Nginx 运行镜像；前端构建仍在离线预构建阶段完成。
# 旧版 docker-compose 使用单一宿主 build context，目录内仅放本文件、配置和 dist。
ARG RUNTIME_BASE_IMAGE
FROM ${RUNTIME_BASE_IMAGE}

WORKDIR /usr/share/nginx/html

# 删除运行镜像自带页面，避免旧静态资源残留后再复制本次预构建产物。
RUN rm -rf /usr/share/nginx/html/*

# 预构建 context 由发布准备步骤提供，不触发 Node 依赖下载。
COPY dist /usr/share/nginx/html
COPY nginx.frontend.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
