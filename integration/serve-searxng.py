"""Serve the upstream SearXNG Flask app with the existing Waitress WSGI server."""
from waitress import serve
from searx.webapp import app

serve(app, host="127.0.0.1", port=8888, threads=12)
