# Multi-stage build: compile the Vite app, then serve the static bundle with
# nginx (which also reverse-proxies /api and /uploads to the backend container
# — see nginx.conf). Build context is the frontend/ directory itself.

FROM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG VITE_API_URL=/api/v1
RUN VITE_API_URL="$VITE_API_URL" npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/nginx.conf
EXPOSE 80
