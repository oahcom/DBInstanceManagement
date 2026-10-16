import logging
import time

logger = logging.getLogger("inventory.request")


class RequestTimingMiddleware:
    """统计每个请求的处理耗时，写日志 + 回填 X-Request-Duration-Ms 响应头。

    耗时取值放在 get_response 两侧而不是 process_request/process_response，因为
    后者测不到视图内部的异常被 ExceptionMiddleware 转成 500 的那段时间。
    """

    HEADER = "X-Request-Duration-Ms"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.monotonic()
        response = self.get_response(request)
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        response[self.HEADER] = f"{elapsed_ms:.2f}"
        logger.info(
            "%s %s -> %s %.2fms",
            request.method,
            request.get_full_path(),
            response.status_code,
            elapsed_ms,
        )
        return response
