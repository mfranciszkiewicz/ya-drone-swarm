FROM alpine:3.14

ENV PATH="/yagna/bin:/root/.local/bin:$PATH"
ENV RUST_LOG="debug"

COPY ya-install.sh .

RUN apk add --no-cache curl bash jq xmlstarlet util-linux coreutils && \
    apk add --no-cache -X http://dl-cdn.alpinelinux.org/alpine/edge/testing grep && \
    mkdir -p /yagna/bin

RUN script -qfc "bash ya-install.sh"
RUN apk del util-linux

CMD ["/bin/bash"]
