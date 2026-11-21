# The scorer builds this on linux/amd64 and runs it with no network.

# Stage 1: sqlglot's own repository at the pinned commit (git image from Docker Hub).
FROM alpine/git:v2.49.1@sha256:c0280cf9572316299b08544065d3bf35db65043d5e3963982ec50647d2746e26 AS sqlglot-src
ARG SQLGLOT_COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN git init -q /src && cd /src \
 && git remote add origin https://github.com/tobymao/sqlglot.git \
 && git fetch -q --depth 1 origin "$SQLGLOT_COMMIT" \
 && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "$SQLGLOT_COMMIT"

# Stage 2: the image the commits were labeled in (same Python and SQLite).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
COPY --from=sqlglot-src /src /opt/sqlglot
# Compile once so imports at run time read bytecode; the run-time disk is read-only.
RUN rm -rf /opt/sqlglot/.git && python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests

COPY blast /app/blast
COPY tools /app/tools
# Trace every test of the pinned commit to build the line/class/file maps (about 2 minutes).
RUN python /app/tools/build_maps.py --root /opt/sqlglot --out /app/maps \
 && python -m compileall -q /app/blast && chmod -R a+rX /app /opt/sqlglot

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "-m", "blast"]
WORKDIR /app
