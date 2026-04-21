FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir flask==3.0.3 prometheus-client==0.21.0

COPY app.py .

RUN useradd --create-home --shell /bin/bash app && chown -R app:app /app
USER app

EXPOSE 8080
CMD ["python", "app.py"]
