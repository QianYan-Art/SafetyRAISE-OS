# 仅用于已核验的本机运行镜像；全新安装仍使用backend.Dockerfile。
ARG RUNTIME_BASE_IMAGE
FROM ${RUNTIME_BASE_IMAGE} AS verified-runtime

FROM verified-runtime

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/backend
ENV PIP_NO_CACHE_DIR=1
ENV SAFETYRAISE_TOKEN_ACCEL_LIB=/app/backend/native/query_token_accel/libquery_token_accel.so

WORKDIR /app

# 删除的是新镜像内的应用目录，不触碰宿主业务数据或运行容器。
RUN rm -rf /app/backend /app/frontend

COPY backend /app/backend
COPY frontend/src /app/frontend/src
COPY frontend/package.json frontend/package-lock.json /app/frontend/
COPY --from=verified-runtime /app/backend/native/query_token_accel/libquery_token_accel.so /app/backend/native/query_token_accel/libquery_token_accel.so
COPY deployment/docker/backend-entrypoint.sh /usr/local/bin/backend-entrypoint.sh
COPY deployment/docker/verify-runtime-dependencies.py /tmp/verify-runtime-dependencies.py

RUN python /tmp/verify-runtime-dependencies.py \
    && python -m pip check \
    && chmod +x /usr/local/bin/backend-entrypoint.sh \
    && rm /tmp/verify-runtime-dependencies.py

WORKDIR /app/backend

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/backend-entrypoint.sh"]
CMD ["python", "-m", "app.main", "serve", "--host", "0.0.0.0", "--port", "8000", "--config", "config/workflow.server.yaml"]
