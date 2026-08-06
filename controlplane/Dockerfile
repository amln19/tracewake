FROM golang:1.24.12 AS build
WORKDIR /src
COPY controlplane/go.mod controlplane/go.sum ./
RUN go mod download
COPY controlplane/ ./
RUN CGO_ENABLED=0 go build -o /out/locusd ./cmd/locusd

FROM alpine:3.22
RUN addgroup -S -g 10001 locus && adduser -S -u 10001 -G locus locus \
    && mkdir -p /run/locus /var/lib/locus \
    && chown -R locus:locus /run/locus /var/lib/locus
USER locus
COPY --from=build /out/locusd /usr/local/bin/locusd
ENTRYPOINT ["locusd"]
