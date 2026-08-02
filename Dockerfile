# Stage 1: build the React frontend bundle
FROM node:22-slim AS frontend-build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: Python application
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=frontend-build /app/dist ./frontend/dist
RUN pip install --no-cache-dir .

ENV DASHBOARD_SPA_DIST=/app/frontend/dist

CMD ["agentg"]
