"""Event handler decorators"""


from .. import infrastructure as inf
from ..lang.func import FuncModifier, Func, Foreign


def make_response(status, body, headers, multi_value_headers=None) -> dict:
    """Make a response dict for AWS Lambda"""
    if not multi_value_headers:
        multi_value_headers = {}

    return dict(
        statusCode=status,
        body=body,
        headers=headers,
        multiValueHeaders=multi_value_headers,
    )


@Foreign
def OkJson(value):
    """HTTP 200 with a JSON response"""
    body = json.dumps(value)
    return make_response(200, body, {"content-type": "application/json"})


# headers["content-type"] = "text/html"


@Foreign
def Error(value):
    """Generic HTTP 500"""
    return make_response(500, "Error", {})


class HttpHandler(FuncModifier):
    """Add an HttpEndpoint infrastructure resource to func"""

    def __init__(self, method: str, path: str):
        self.method = method
        self.path = path

    def modify(self, fn: Func):
        endpoint_name = self.method + "_" + self.path.replace("/", "_")
        fn.infrastructure.extend(
            [
                # The Endpoint handler is the Function name!
                inf.HttpEndpoint(endpoint_name, self.method, self.path, fn.__name__),
                inf.Function(fn.__name__),
            ]
        )
        return fn
