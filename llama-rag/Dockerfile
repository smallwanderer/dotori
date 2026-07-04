FROM ghcr.io/ggml-org/llama.cpp:server

ENV TZ=Asia/Seoul
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY start.sh /usr/local/bin/dotori-llama-start
RUN chmod +x /usr/local/bin/dotori-llama-start
ENTRYPOINT ["/usr/local/bin/dotori-llama-start"]
