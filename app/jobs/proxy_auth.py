"""Crawlee adapter: turn upstream auth errors into queue deferrals before retries.

The adaptive static pipeline must be guarded before it decides to retry in a
browser. Both adapters use Crawlee's existing RequestThrottledError path, which
reclaims the same request without marking it handled or spending a retry/rotation.
"""

from crawlee.errors import ContextPipelineInitializationError

from app.fetching.proxy_auth import is_proxy_auth_failure


def guard_proxy_auth(crawler, runner, *, adaptive: bool) -> None:
    def rejected(context, error):
        # Chromium may report only ERR_HTTP_RESPONSE_CODE_FAILURE after a 407.
        # The native guard marks this shared request only on an explicit proxy
        # challenge rejection/407; ambiguous error text alone never qualifies.
        corroborated = context.request.user_data.pop("_wss_proxy_auth_failed", False)
        return (runner.tiers.enabled and corroborated) or is_proxy_auth_failure(error, proxy_used=runner.tiers.enabled)

    async def guarded(context, operation):
        try:
            return await operation
        except Exception as error:
            if not rejected(context, error):
                raise
            await runner._pause_for_proxy_auth()
            from app.jobs.runner import Deferred

            # Replace the transport exception before Crawlee can log credentials,
            # rotate a session, retry a page or fall through from HTTP to browser.
            raise ContextPipelineInitializationError(
                Deferred("upstream proxy authentication requires Resume"), context
            ) from None

    # BasicCrawler calls this before marking an exhausted request handled. Keep
    # a defensive pre-retry guard as well as the navigation pipeline guard.
    original_error_handler = crawler._handle_request_error

    async def handle_error(context, error):
        if rejected(context, error):
            await runner._pause_for_proxy_auth()
            from app.jobs.runner import Deferred

            await crawler._defer_throttled_request(
                context.request, Deferred("upstream proxy authentication requires Resume")
            )
            return
        await original_error_handler(context, error)

    crawler._handle_request_error = handle_error

    if adaptive:
        original = crawler._pipeline_call_factory

        def pipeline(rendering_type, context_linked_to_result):
            return guarded(context_linked_to_result, original(rendering_type, context_linked_to_result))

        crawler._pipeline_call_factory = pipeline
    else:
        original = crawler._run_request_handler

        async def handle(context):
            return await guarded(context, original(context))

        crawler._run_request_handler = handle
