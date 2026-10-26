"""
serve.py  -  Local HTTP server for the CA Fish Reg Map.

Usage:
    python serve.py

Finds a free port (starting at 8765), serves this folder on it, and opens
the map in your browser. Keep this window open while using the map.
"""

import http.server
import os
import socket
import socketserver
import sys
import webbrowser

START_PORT = 8765
DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(DIR)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        # On Windows, refuse to share a port another program is already using
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


httpd = None
for port in range(START_PORT, START_PORT + 50):
    if port_in_use(port):
        print(f"Port {port} is already used by another program, trying the next one...")
        continue
    try:
        httpd = Server(("127.0.0.1", port), http.server.SimpleHTTPRequestHandler)
        break
    except OSError:
        print(f"Port {port} is not available, trying the next one...")

if httpd is None:
    sys.exit("Could not find a free port.")

url = f"http://localhost:{port}/index.html"
print(f"\nServing CA Fish Reg Map from {DIR}")
print(f"Open {url}")
print("Press Ctrl+C to stop.\n")
webbrowser.open(url)
try:
    httpd.serve_forever()
except KeyboardInterrupt:
    print("Stopped.")
