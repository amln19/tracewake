FROM node:26.5.0-alpine AS dashboard
WORKDIR /src/dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

FROM golang:1.24.12 AS build
WORKDIR /src
COPY controlplane/go.mod controlplane/go.sum ./
RUN go mod download
COPY controlplane/ ./
RUN CGO_ENABLED=0 go build -o /out/tracewaked ./cmd/tracewaked

FROM alpine:3.22
RUN addgroup -S -g 10001 tracewake && adduser -S -u 10001 -G tracewake tracewake \
    && mkdir -p /run/tracewake /var/lib/tracewake \
    && chown -R tracewake:tracewake /run/tracewake /var/lib/tracewake
USER tracewake
COPY --from=build /out/tracewaked /usr/local/bin/tracewaked
COPY --from=dashboard /src/dashboard/dist /usr/share/tracewake/dashboard
ENV TRACEWAKE_DASHBOARD_DIR=/usr/share/tracewake/dashboard
ENTRYPOINT ["tracewaked"]
