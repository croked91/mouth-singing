FROM node:20-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html

# SPA routing: serve index.html for all non-file routes
RUN printf 'client_max_body_size 100m;\n\nserver {\n  listen 80;\n  location / {\n    root /usr/share/nginx/html;\n    try_files $uri $uri/ /index.html;\n  }\n  location /api/ {\n    proxy_pass http://backend:8000/api/;\n    proxy_set_header Host $host;\n    proxy_set_header X-Real-IP $remote_addr;\n    proxy_read_timeout 600s;\n    proxy_send_timeout 600s;\n    proxy_buffering off;\n    client_max_body_size 100m;\n  }\n  location /health {\n    proxy_pass http://backend:8000/health;\n  }\n}\n' > /etc/nginx/conf.d/default.conf

EXPOSE 80
