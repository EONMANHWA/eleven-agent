FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 bot
COPY --chown=bot:bot app.py eleven_http.py checkpoint.py ./
USER bot
EXPOSE 10000
CMD ["python", "app.py"]
