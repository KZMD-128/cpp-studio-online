FROM docker:27-cli
RUN apk add --no-cache python3
WORKDIR /app
COPY api.py /app/api.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python3", "/app/api.py"]
