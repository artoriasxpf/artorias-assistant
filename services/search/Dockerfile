FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.141.1 uvicorn==0.52.1 jieba==0.42.1
COPY src/artorias_search /app/artorias_search
COPY src/artorias_search_service /app/artorias_search_service
ENV ARTORIAS_SEARCH_DB=/data/search.sqlite
RUN useradd --uid 10001 --create-home search && mkdir /data && chown search /data
USER search
VOLUME /data
EXPOSE 8766
CMD ["python", "-m", "artorias_search_service.main", "--host", "0.0.0.0", "--port", "8766"]
