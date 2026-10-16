"""Tiny Flask stand-in so the web UI runs without Flask (simulator, tests).

Only what web_app.py uses: route(), request (method/args/form/files/get_json),
jsonify, Response, send_file. Fixed paths only. The Pi uses real Flask.
"""
from email.parser import BytesParser
from email.policy import HTTP
import json
import threading
from urllib.parse import parse_qs

_local = threading.local()
_STATUS = {200: "OK", 204: "No Content", 400: "Bad Request", 404: "Not Found",
           405: "Method Not Allowed", 500: "Internal Server Error"}


class Response:
    def __init__(self, body=b"", status=200, mimetype="text/html", headers=None):
        self.body = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        self.status = status
        self.mimetype = mimetype
        self.headers = dict(headers or {})

    @property
    def data(self):
        return self.body

    def get_json(self):
        return json.loads(self.body.decode("utf-8"))


def jsonify(obj):
    return Response(json.dumps(obj, ensure_ascii=False), mimetype="application/json")


def send_file(path, mimetype="application/octet-stream", max_age=None):
    with open(path, "rb") as f:
        r = Response(f.read(), mimetype=mimetype)
    if max_age is not None:
        r.headers["Cache-Control"] = f"max-age={int(max_age)}" if max_age else "no-store"
    return r


class _Upload:
    def __init__(self, filename, data):
        self.filename = filename
        self._data = data

    def read(self):
        return self._data


class _Request:
    """The request of the current thread (set by the app for each call)."""

    def _env(self):
        return _local.environ

    @property
    def method(self):
        return self._env().get("REQUEST_METHOD", "GET")

    @property
    def path(self):
        return self._env().get("PATH_INFO", "/")

    @property
    def args(self):
        q = parse_qs(self._env().get("QUERY_STRING", ""))
        return {k: v[0] for k, v in q.items()}

    def _body(self):
        if not hasattr(_local, "body"):
            env = self._env()
            try:
                n = int(env.get("CONTENT_LENGTH") or 0)
            except ValueError:
                n = 0
            _local.body = env["wsgi.input"].read(n) if n > 0 else b""
        return _local.body

    def get_json(self, silent=False):
        try:
            return json.loads(self._body().decode("utf-8"))
        except Exception:
            if silent:
                return None
            raise

    def _multipart(self):
        if not hasattr(_local, "parts"):
            form, files = {}, {}
            ctype = self._env().get("CONTENT_TYPE", "")
            if ctype.startswith("multipart/form-data"):
                head = b"Content-Type: " + ctype.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n"
                msg = BytesParser(policy=HTTP).parsebytes(head + self._body())
                for part in msg.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    filename = part.get_filename()
                    data = part.get_payload(decode=True) or b""
                    if filename is not None:
                        files[name] = _Upload(filename, data)
                    elif name:
                        form[name] = data.decode("utf-8", "replace")
            elif ctype.startswith("application/x-www-form-urlencoded"):
                form = {k: v[0] for k, v in parse_qs(self._body().decode("utf-8")).items()}
            _local.parts = (form, files)
        return _local.parts

    @property
    def form(self):
        return self._multipart()[0]

    @property
    def files(self):
        return self._multipart()[1]


request = _Request()


class Flask:
    def __init__(self, name=""):
        self.name = name
        self.routes = {}                    # path -> {method: view}

    def route(self, path, methods=("GET",)):
        def register(view):
            for m in methods:
                self.routes.setdefault(path, {})[m.upper()] = view
            return view
        return register

    def _dispatch(self, environ):
        views = self.routes.get(environ.get("PATH_INFO", "/"))
        if views is None:
            return Response("not found", 404, "text/plain")
        view = views.get(environ.get("REQUEST_METHOD", "GET"))
        if view is None:
            return Response("method not allowed", 405, "text/plain")
        _local.environ = environ
        for attr in ("body", "parts"):
            if hasattr(_local, attr):
                delattr(_local, attr)
        try:
            result = view()
        except Exception as e:              # a broken view must not take the server down
            import traceback
            traceback.print_exc()
            return Response(json.dumps({"ok": False, "error": str(e)}), 500, "application/json")
        status = 200
        if isinstance(result, tuple):
            result, status = result[0], result[1]
        if not isinstance(result, Response):
            result = Response(result if result is not None else b"")
        if status != 200:
            result.status = status
        return result

    def __call__(self, environ, start_response):
        r = self._dispatch(environ)
        headers = [("Content-Type", r.mimetype + ("; charset=utf-8" if r.mimetype.startswith("text/")
                                                  or r.mimetype == "application/json" else "")),
                   ("Content-Length", str(len(r.body)))]
        headers += list(r.headers.items())
        start_response(f"{r.status} {_STATUS.get(r.status, 'OK')}", headers)
        return [r.body]

    # for tests: call a route without a server
    def call(self, method, path, json_body=None, query="", body=b"", content_type=""):
        import io
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            content_type = "application/json"
        environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
                   "CONTENT_LENGTH": str(len(body)), "CONTENT_TYPE": content_type,
                   "wsgi.input": io.BytesIO(body)}
        return self._dispatch(environ)
