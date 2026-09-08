FROM python:3.12-slim

# geos is needed by shapely for the boundary step (build_geo.py)
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgeos-c1v5 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py entrypoint.sh schema.sql ./
COPY tools/ ./tools/
COPY tests/ ./tests/
COPY web/ ./web/
# also strip CRLF if the image is ever built from a Windows checkout without
# .gitattributes applied - a \r on the shebang makes the script unrunnable
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

# raw/, out/ and reports/ are bind-mounted from the host by docker-compose
RUN mkdir -p raw out reports

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["build"]
