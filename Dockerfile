FROM node:20-alpine AS ui-build
WORKDIR /ui
COPY digest-ui/package.json digest-ui/package-lock.json ./
RUN npm ci
COPY digest-ui/ ./
RUN npm run build

FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py server.py ./
COPY --from=ui-build /ui/dist ./digest-ui/dist
EXPOSE 8000
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
