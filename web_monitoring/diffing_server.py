import concurrent.futures
from docopt import docopt
import hashlib
from importlib import import_module
import inspect
import json
import tornado.gen
import tornado.httpclient
import tornado.ioloop
import tornado.web


def load_config(config):
    """

    Example
    -------

    >>> load_config({'foo', ('mypackage.mymodule', 'foofunc')})
    """
    d = {}
    for name, spec in config.items():
        modname, funcname = spec
        mod = import_module(modname)
        func = getattr(mod, funcname)
        d[name] = func
    return d


client = tornado.httpclient.AsyncHTTPClient()


class DiffHandler(tornado.web.RequestHandler):
    # subclass must define `differs` attribute

    @tornado.gen.coroutine
    def get(self, differ):
        # Find the diffing function registered with the name given by `differ`.
        try:
            func = self.differs[differ]
        except KeyError:
            self.send_error(404)
            return

        # If params repeat, take last one. Decode bytes into unicode strings.
        query_params = {k: v[-1].decode() for k, v in
                        self.request.arguments.items()}
        a = query_params.pop('a')
        b = query_params.pop('b')

        # Fetch server response for URLs a and b.
        res_a, res_b = yield [client.fetch(a), client.fetch(b)]

        # Validate response bytes against hash, if provided.
        for query_param, res in zip(('a_hash', 'b_hash'), (res_a, res_b)):
            try:
                expected_hash = query_params.pop('a_hash')
            except KeyError:
                # No hash provided in the request. Skip validation.
                pass
            else:
                actual_hash = hashlib.sha256(res.body).hexdigest()
                if actual_hash != expected_hash:
                    self.send_error(
                        500, reason="Fetched content does not match hash.")
                    return

        # TODO Add caching of fetched URIs.

        # Pass the bytes and any remaining args to the diffing function.
        executor = concurrent.futures.ProcessPoolExecutor()
        res = yield executor.submit(caller, func, res_a, res_b, **query_params)
        self.write({'diff': res})


def _extract_encoding(headers):
    content_type = headers["Content-Type"]
    if 'charset=' in content_type:
        return content_type.split('charset=')[-1]
    else:
        return None

def caller(func, a, b, **query_params):
    """
    A translation layer between HTTPResponses and differ functions.

    Parameters
    ----------
    func : callable
        a 'differ' function
    a : tornado.httpclient.HTTPResponse
    b : tornado.httpclient.HTTPResponse
    **query_params
        additional parameters parsed from the REST diffing request


    The function `func` may expect required and/or optional arguments. Its
    signature serves as a dependency injection scheme, specifying what it
    needs from the HTTPResponses. The following argument names have special
    meaning:

    * a_url, b_url: URL of HTTP request
    * a_body, b_body: Raw HTTP reponse body (bytes)
    * a_text, b_text: Decoded text of HTTP response body (str)

    Any other argument names in the signature will take their values from the
    REST query parameters.
    """
    # Supplement the query_parameters from the REST call with special items
    # extracted from `a` and `b`.
    query_params.setdefault('a_url', a.request.url)
    query_params.setdefault('b_url', b.request.url)
    query_params.setdefault('a_body', a.body)
    query_params.setdefault('b_body', b.body)
    a_encoding = _extract_encoding(a.headers) or 'UTF-8'
    b_encoding = _extract_encoding(a.headers) or 'UTF-8'
    query_params.setdefault('a_text', a.body.decode(a_encoding, errors='ignore'))
    query_params.setdefault('b_text', b.body.decode(b_encoding, errors='ignore'))

    # The differ's signature is a dependency injection scheme.
    kwargs = dict()
    sig = inspect.signature(func)
    for name, param in sig.parameters.items():
        try:
            kwargs[name] = query_params[name]
        except KeyError:
            if param.default is inspect._empty:
                # This is a required argument.
                raise KeyError("{} requires a parameter {} which was not "
                               "provided in the query"
                               "".format(func.__name__, name))
    return func(**kwargs)


def make_app(config):

    class BoundDiffHandler(DiffHandler):
        differs = load_config(config)

    return tornado.web.Application([
        (r"/([A-Za-z0-9_]+)", BoundDiffHandler),
    ])

def start_app(config, port):
    app = make_app(config)
    app.listen(port)
    print(f'Starting server on port {port}')
    tornado.ioloop.IOLoop.current().start()


def cli():
    doc = """Start a diffing server.

Usage:
wm-diffing-server <config_file> [--port <port>]

Options:
-h --help     Show this screen.
--version     Show version.
--port        Port. [default: 8888]
"""
    arguments = docopt(doc, version='0.0.1')
    with open(arguments['<config_file>']) as f:
        config = json.load(f)
    port = int(arguments['<port>'] or 8888)
    start_app(config, port)
