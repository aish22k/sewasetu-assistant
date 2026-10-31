FROM python:3.11-slim

WORKDIR /srv

# 1) the portal's own libraries (we reuse the portal's rules and database helper)
COPY app/requirements.txt /tmp/portal-requirements.txt
RUN pip install --no-cache-dir -r /tmp/portal-requirements.txt

# 2) the MCP library, and the AI library used only by the evals (run_evals.py)
RUN pip install --no-cache-dir "mcp>=1.10,<2" "openai>=1.40"

# 3) a copy of the portal code (read-only use: scheme rules, database connection)
COPY app /srv/portal
ENV PYTHONPATH=/srv/portal

# 4) our assistant server and its eval page
COPY assistant/server.py /srv/server.py
COPY assistant/dashboard.py /srv/dashboard.py

# 5) the eval files (also mounted from the project folder when you run them by hand)
COPY assistant /srv/assistant

EXPOSE 9000
CMD ["python", "/srv/server.py"]
