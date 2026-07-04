FROM vllm/vllm-openai:latest

ENV TZ=Asia/Seoul

COPY start.sh /usr/local/bin/dotori-vllm-start
RUN chmod +x /usr/local/bin/dotori-vllm-start

ENTRYPOINT ["/usr/local/bin/dotori-vllm-start"]
