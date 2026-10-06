FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1
# Heure des journaux du conteneur ; JARVIS calcule ses horaires (Paris, New York) indépendamment
ENV TZ=Europe/Paris

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# data/ garde la dernière valeur connue et l'état des alertes : monté en volume pour survivre aux redémarrages
VOLUME /app/data

EXPOSE 8000
# Santé du serveur sans curl (absent de l'image slim) : `docker ps` affiche healthy / unhealthy
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5).status == 200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
