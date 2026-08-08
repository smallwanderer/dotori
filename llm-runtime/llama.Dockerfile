FROM ghcr.io/ggml-org/llama.cpp:server

ENV TZ=Asia/Seoul
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

ENV RAG_RUNTIME_EXEC="/app/llama-server"
COPY start.sh /usr/local/bin/dotori-rag-start
RUN chmod +x /usr/local/bin/dotori-rag-start
ENTRYPOINT ["/usr/local/bin/dotori-rag-start"]
