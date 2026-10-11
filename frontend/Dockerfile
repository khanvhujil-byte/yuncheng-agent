# 前端镜像：两段构建 —— node 里打包，nginx 里托管。
# 构建上下文是**仓库根目录**（因为要一并拿到 deploy/nginx.conf）。

FROM node:22-alpine AS build
WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./

# 容器里前后端同源（nginx 反代 /api 到 backend），所以默认留空即可；
# 需要指向别处时用 --build-arg VITE_API_BASE_URL=http://host:port 覆盖。
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginx:1.27-alpine
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
