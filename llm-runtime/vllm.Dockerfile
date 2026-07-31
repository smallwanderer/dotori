FROM vllm/vllm-openai:latest

ENV TZ=Asia/Seoul
ENV RAG_RUNTIME_EXEC="python3 -m vllm.entrypoints.openai.api_server"

COPY start.sh /usr/local/bin/dotori-rag-start
RUN chmod +x /usr/local/bin/dotori-rag-start

ENTRYPOINT ["/usr/local/bin/dotori-rag-start"]
