"""Serve the upstream SearXNG Flask app with Waitress."""
from waitress import serve
from searx.webapp import app

from runtime_config import RESOLVED, read_json
from urllib.parse import urlsplit

address = urlsplit(read_json(RESOLVED)["searxng_url"])
serve(app, host=address.hostname, port=address.port, threads=12)
