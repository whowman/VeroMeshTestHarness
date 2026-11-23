FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd -u 1000 harness
USER 1000
EXPOSE 8100
ENTRYPOINT ["harness"]
CMD ["serve", "--people", "2000", "--seed", "7", "--port", "8100"]
