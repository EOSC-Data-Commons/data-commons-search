"""HTTP API to deploy the EOSC Data Commons search agent."""

import contextlib
import json
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Generator
from datetime import datetime, timezone
from typing import Any

from ag_ui.core import (
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageChunkEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from langchain.messages import AIMessage, AIMessageChunk, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.callbacks import Callbacks
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from langfuse import Langfuse, propagate_attributes
from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.auth.routes import create_protected_resource_routes
from mcp.types import TextContent
from pydantic import AnyHttpUrl
from starlette.middleware.authentication import AuthenticationMiddleware

from data_commons_search.auth import (
    EgiTokenVerifier,
    UserInfo,
    apply_pending_auth_cookies,
    oidc_issuer_url,
    optional_auth,
    require_auth,
)
from data_commons_search.auth import router as auth_router
from data_commons_search.config import settings
from data_commons_search.db import (
    delete_conversations,
    get_conversation,
    get_conversations,
    init_postgres_storage,
    store_messages,
)
from data_commons_search.logging import BLUE, BOLD, RESET, setup_logging
from data_commons_search.mcp_server import mcp
from data_commons_search.models import (
    AgentInput,
    ConversationDetail,
    ConversationSummary,
    DbStats,
    LangChainResponseMetadata,
    MessageItem,
    RerankingOutput,
    RerankingOutputResponse,
    SearchResults,
    SummarizedSearchResponse,
    TextPart,
    TokenUsageMetadata,
    ToolCallItem,
    ToolResultItem,
)
from data_commons_search.prompts import RERANK_PROMPT, TOOL_CALL_PROMPT
from data_commons_search.rate_limit import RateLimiter
from data_commons_search.stats import load_stats
from data_commons_search.utils import (
    file_logger,
    get_system_prompt,
    load_chat_model_with_fallback,
    logger,
    sse,
)
from data_commons_search.vault import router as vault_router

# Configure logging in code (not via uvicorn --log-config) so the format is consistent
# however the app is launched. JSON Lines in prod/staging (LOG_JSON=true) for ELK, rich otherwise.
setup_logging(json_logs=settings.log_json, level=settings.log_level, debug=settings.debug_enabled)

rate_limiter = RateLimiter()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan that initializes the MCP session manager."""
    init_postgres_storage()
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="EOSC Data Commons Search API",
    description="A server for the [EOSC Data Commons project](https://eosc.eu/horizon-europe-projects/eosc-data-commons/) MatchMaker service, providing natural language search over open-access datasets. It exposes an HTTP POST endpoint and supports the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) to help users discover datasets and tools via a Large Language Model-assisted search.",
    version="1.0.0",
    lifespan=lifespan,
    # root_path=settings.root_path,
)

# Mount the MCP server with optional OAuth: a valid EGI bearer token is decoded and exposed to tools, but auth is NOT required
mcp_app = mcp.streamable_http_app()
# add_middleware prepends, so the last one added runs outermost: AuthenticationMiddleware populates
# scope["user"], then AuthContextMiddleware copies it into the contextvar tools read.
mcp_app.add_middleware(AuthContextMiddleware)
mcp_app.add_middleware(AuthenticationMiddleware, backend=BearerAuthBackend(EgiTokenVerifier()))
app.mount("/mcp", mcp_app, name="mcp")

# Advertise OAuth 2.0 Protected Resource Metadata (RFC 9728) at /.well-known/oauth-protected-resource/mcp
# so MCP clients can discover EGI AAI as the auth server and run standard OAuth flow
_mcp_resource = settings.mcp_resource_url or settings.api_public_url or settings.server_url
if _mcp_resource:
    app.router.routes.extend(
        create_protected_resource_routes(
            resource_url=AnyHttpUrl(f"{_mcp_resource.rstrip('/')}/mcp"),
            authorization_servers=[AnyHttpUrl(oidc_issuer_url())],
            resource_name=settings.app_name,
        )
    )


if settings.cors_enabled:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def build_mcp_client(access_token: str | None = None) -> MultiServerMCPClient:
    """Build an MCP client for the in-process server.

    When the caller is authenticated, forward their EGI access token as a Bearer header so the
    MCP server can resolve the user (used by tools to log/personalize). The internal MCP calls
    are plain HTTP requests that do not inherit the browser's auth cookie, so the token must be
    passed explicitly here.
    """
    connection: StreamableHttpConnection = {
        "url": f"{settings.server_url}/mcp",
        "transport": "streamable_http",
    }
    if access_token:
        connection["headers"] = {"Authorization": f"Bearer {access_token}"}
    return MultiServerMCPClient({"data-commons-search": connection})


# Initialize Langfuse client with host from settings (keys still come from env vars)
langfuse = Langfuse(
    host=settings.langfuse_base_url, public_key=settings.langfuse_public_key, secret_key=settings.langfuse_secret_key
)

logger.info(f"""🔭 {BOLD}{BLUE}EOSC Data Commons Search API{RESET} · {BOLD}{settings.server_url}{RESET}
⚡️ Streamable HTTP MCP server · {settings.server_url}/mcp
🔓 Login · {settings.server_url}/auth/login
🐘 PostgreSQL · {BOLD}{settings.postgres_host}{RESET}
🔎 OpenSearch · {BOLD}{settings.opensearch_url}{RESET}""")


@app.post("/chat")
async def chat_endpoint(
    request: Request, search_input: AgentInput, user: UserInfo | None = Depends(optional_auth)
) -> StreamingResponse:
    """Natural language search."""
    if user:
        logger.info(f"Logged in with user: {user.preferred_username or user.sub}")
    await rate_limiter.check(request, user)

    auth_header = request.headers.get("Authorization", "")
    if settings.chat_api_key and (not auth_header or not auth_header.startswith("Bearer ")):
        raise ValueError("Missing or invalid Authorization header")
    if settings.chat_api_key and auth_header.split(" ")[1] != settings.chat_api_key:
        raise ValueError("Invalid API key")

    # Resolve/generate thread_id before streaming so we can expose it as a header.
    # This lets clients capture it from HTTP headers rather than parsing the SSE stream.
    if search_input.thread_id is None:
        search_input.thread_id = uuid.uuid4().hex

    # Forward the validated EGI access token (set by optional_auth) to the MCP calls so tools
    # can resolve the logged-in user. None for anonymous callers.
    access_token = getattr(request.state, "access_token", None)
    response = StreamingResponse(
        stream_chat_response(search_input, user, access_token),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Thread-ID": search_input.thread_id,
        },
    )
    # Make sure refreshed auth cookies are applied to the streaming response
    apply_pending_auth_cookies(request, response)
    return response


def get_timestamp() -> int:
    """Get the current UTC timestamp in seconds."""
    return int(time.time())
    # return int(datetime.now(timezone.utc).timestamp())


def _truncate_hits(data: Any, limit: int = 10) -> Any:
    """If the payload looks like a search result with a `hits` list, cap it to `limit`."""
    if isinstance(data, dict) and isinstance(data.get("hits"), list) and len(data["hits"]) > limit:
        return {**data, "hits": data["hits"][:limit]}
    return data


def _parse_tool_result(res: Any) -> tuple[Any, str]:
    """Extract `(parsed_json_or_none, raw_text)` from an MCP tool call result."""
    if res.structuredContent:
        return res.structuredContent, json.dumps(res.structuredContent)
    if res.content:
        text = "".join(rc.text for rc in res.content if isinstance(rc, TextContent) and rc.text)
        try:
            return json.loads(text), text
        except (json.JSONDecodeError, ValueError):
            return None, text
    return None, ""


def _as_search_results(parsed: Any) -> SearchResults | None:
    """Return `SearchResults` when the parsed payload matches that shape, else None."""
    if parsed is None:
        return None
    try:
        return SearchResults.model_validate(parsed)
    except Exception:
        return None


class ThinkStripper:
    """Removes `<think>...</think>` reasoning blocks from a streamed text.

    Stateful across chunks: the tags may be split across chunk boundaries, so any
    trailing text that could be the start of a tag is buffered until the next chunk.
    Flip `settings.stream_thinking` to forward thinking to the frontend instead.
    """

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self._buffer = ""
        self._in_think = False

    @staticmethod
    def _partial_suffix_len(buffer: str, tag: str) -> int:
        """Length of the longest buffer suffix that is a (partial) prefix of `tag`."""
        for n in range(min(len(buffer), len(tag) - 1), 0, -1):
            if buffer.endswith(tag[:n]):
                return n
        return 0

    def feed(self, delta: str) -> str:
        """Return the visible text in `delta`, withholding anything inside think blocks."""
        self._buffer += delta
        out = ""
        while True:
            if self._in_think:
                idx = self._buffer.find(self.CLOSE)
                if idx == -1:
                    keep = self._partial_suffix_len(self._buffer, self.CLOSE)
                    self._buffer = self._buffer[len(self._buffer) - keep :]
                    break
                self._buffer = self._buffer[idx + len(self.CLOSE) :]
                self._in_think = False
            else:
                idx = self._buffer.find(self.OPEN)
                if idx == -1:
                    keep = self._partial_suffix_len(self._buffer, self.OPEN)
                    cut = len(self._buffer) - keep
                    out += self._buffer[:cut]
                    self._buffer = self._buffer[cut:]
                    break
                out += self._buffer[:idx]
                self._buffer = self._buffer[idx + len(self.OPEN) :]
                self._in_think = True
        return out


class ConversationBuilder:
    """Helper class to build conversation details from messages."""

    def __init__(self, search_input: AgentInput, user: UserInfo | None = None):
        # thread_id is always resolved before ConversationBuilder is created
        self.thread_id = search_input.thread_id or uuid.uuid4().hex
        self.items = search_input.items
        self.model = search_input.model
        self.user = user
        self.msg_id = uuid.uuid4().hex

    # def get_summary(self) -> ConversationSummary:
    #     """Generate a summary for the conversation, e.g. using the first user message."""
    #     first_user_msg = next((item for item in self.items if isinstance(item, MessageItem) and item.role == "user"), None)
    #     label = ""
    #     if first_user_msg:
    #         text_parts = [part.text for part in first_user_msg.content if part.type == "text"]
    #         label = text_parts[0] if text_parts else ""
    #     return ConversationSummary(thread_id=self.conversation_id, label=label)

    def to_langchain(self) -> list[AnyMessage]:
        """Convert conversation items to LangChain messages."""
        new_msgs: list[AnyMessage] = []
        # TODO: add SystemMessage at start?
        for msg in self.items:
            if isinstance(msg, MessageItem):
                text_content = "\n".join(part.text for part in msg.content)
                if msg.role == "user":
                    new_msgs.append(HumanMessage(content=text_content))
                elif msg.role == "assistant":
                    new_msgs.append(AIMessage(content=text_content))
                elif msg.role == "system":
                    new_msgs.append(SystemMessage(content=text_content))
            elif isinstance(msg, ToolResultItem):
                # TODO: how to include the ToolCall?
                new_msgs.append(ToolMessage(content=str(msg.content), tool_call_id=msg.call_id))
            else:
                continue
        return new_msgs

    def add_msg(self, content: str, start_time: int | None = None) -> Generator[str, None]:
        """Emit a complete assistant text message (start → chunk → end) and record it."""
        self.items.append(
            MessageItem(
                id=self.msg_id,
                role="assistant",
                content=[TextPart(text=content)],
                metadata={"model": self.model},
            )
        )
        yield sse(
            TextMessageStartEvent(message_id=self.msg_id, role="assistant", timestamp=start_time or get_timestamp())
        )
        yield sse(TextMessageChunkEvent(delta=content, timestamp=get_timestamp()))
        yield sse(TextMessageEndEvent(message_id=self.msg_id, timestamp=get_timestamp()))

    def start_tool_call(
        self, tool_call_id: str, tool_call_name: str, arguments: dict[str, Any], parent_message_id: str
    ) -> Generator[str, None]:
        """Add a tool call start event to the conversation."""
        self.items.append(
            ToolCallItem(
                id=tool_call_id,
                name=tool_call_name,
                arguments=arguments,
                parent_message_id=parent_message_id,
            )
        )
        yield sse(
            ToolCallStartEvent(
                tool_call_id=tool_call_id,
                tool_call_name=tool_call_name,
                parent_message_id=parent_message_id,
                timestamp=get_timestamp(),
            )
        )
        yield sse(
            ToolCallArgsEvent(
                tool_call_id=tool_call_id,
                delta=json.dumps(arguments),
                timestamp=get_timestamp(),
            )
        )

    def end_tool_call(
        self, tool_call_id: str, tool_call_name: str, msg_id: str, tool_results_str: str
    ) -> Generator[str, None]:
        """Add a tool call end event to the conversation."""
        self.items.append(
            ToolResultItem(
                call_id=tool_call_id,
                content=tool_results_str,
                metadata={"name": tool_call_name, "model": self.model},
            )
        )
        yield sse(
            ToolCallResultEvent(
                message_id=msg_id,
                tool_call_id=tool_call_id,
                content=tool_results_str,
                role="tool",
                timestamp=get_timestamp(),
            )
        )
        yield sse(ToolCallEndEvent(tool_call_id=tool_call_id, timestamp=get_timestamp()))

    def store_messages(self) -> None:
        """Store the conversation messages in the database."""
        if self.user:
            store_messages(user=self.user, thread_id=self.thread_id, items=self.items)


MAX_AGENT_ITERATIONS = 8


async def stream_chat_response(
    search_input: AgentInput, user: UserInfo | None = None, access_token: str | None = None
) -> AsyncGenerator[str, None]:
    """Stream the chat response with an agentic tool-calling loop."""
    run_id = str(uuid.uuid4())
    # Per-request client so the user's bearer token (if any) reaches the MCP server.
    mcp_client = build_mcp_client(access_token)
    token_usage = TokenUsageMetadata()
    t0 = time.monotonic()

    conv = ConversationBuilder(search_input, user)
    initial_items_count = len(conv.items)

    yield sse(RunStartedEvent(thread_id=conv.thread_id, run_id=run_id, timestamp=get_timestamp()))

    final_text: str = ""
    try:
        with (
            langfuse.start_as_current_observation(
                as_type="span",
                name="data-commons-search",
                input={"items": [m.model_dump() for m in search_input.items]},
                metadata={"model": search_input.model, "run_id": run_id},
            ),
            propagate_attributes(
                session_id=search_input.thread_id,
                metadata={"model": search_input.model, "run_id": run_id},
            ),
        ):
            langfuse_handler = LangfuseCallbackHandler()
            tools = await mcp_client.get_tools()
            # Rate-limit (HTTP 429) on the primary provider transparently falls back to
            # settings.fallback_llm_model. bind_tools is applied to both models.
            llm_with_tools, used_model = load_chat_model_with_fallback(
                search_input.model,
                lambda m: m.bind_tools(tools),
                callbacks=[langfuse_handler],
            )

            lc_msgs: list[AnyMessage] = [get_system_prompt(TOOL_CALL_PROMPT), *conv.to_langchain()]

            async with mcp_client.session("data-commons-search") as session:
                for _iter in range(MAX_AGENT_ITERATIONS):
                    # Stream LLM response, accumulating chunks and forwarding text deltas
                    msg_id = uuid.uuid4().hex
                    conv.msg_id = msg_id
                    accumulated: AIMessageChunk | None = None
                    text_started = False
                    iter_text = ""
                    start_time = get_timestamp()
                    # Hide <think>...</think> reasoning unless the frontend opts in via settings
                    stripper = None if settings.stream_thinking else ThinkStripper()
                    async for chunk in llm_with_tools.astream(lc_msgs):
                        if not isinstance(chunk, AIMessageChunk):
                            continue
                        accumulated = chunk if accumulated is None else accumulated + chunk
                        delta = chunk.content if isinstance(chunk.content, str) else ""
                        if stripper is not None:
                            delta = stripper.feed(delta)
                        if delta:
                            if not text_started:
                                yield sse(
                                    TextMessageStartEvent(message_id=msg_id, role="assistant", timestamp=start_time)
                                )
                                text_started = True
                            iter_text += delta
                            yield sse(TextMessageChunkEvent(delta=delta, timestamp=get_timestamp()))

                    if text_started:
                        yield sse(TextMessageEndEvent(message_id=msg_id, timestamp=get_timestamp()))
                    if accumulated is None:
                        break

                    # Track token usage
                    with contextlib.suppress(Exception):
                        token_usage += LangChainResponseMetadata.model_validate(
                            accumulated.response_metadata
                        ).token_usage

                    # Persist any assistant text in the conversation history
                    if iter_text:
                        conv.items.append(
                            MessageItem(
                                id=msg_id,
                                role="assistant",
                                content=[TextPart(text=iter_text)],
                                metadata={"model": conv.model},
                            )
                        )
                        final_text = iter_text

                    tool_calls = list(accumulated.tool_calls or [])
                    if not tool_calls:
                        break
                    # Append assistant message (with tool_calls) so the model sees its own turn
                    lc_msgs.append(AIMessage(content=accumulated.content or "", tool_calls=tool_calls))
                    # Collects summaries of rerank steps for logging purpose
                    rerank_summaries: list[str] = []
                    # Flips True for anything that isn't a reranked search
                    needs_synthesis = False
                    for tool_call in tool_calls:
                        tool_call_id = str(tool_call.get("id") or f"call_{uuid.uuid4().hex}")
                        tool_call_name = str(tool_call["name"])
                        tool_call_args = dict(tool_call["args"])
                        try:
                            logger.info(f"Calling tool '{tool_call_name}' · \"{tool_call_args}\"", extra=tool_call_args)
                        except Exception:
                            logger.info(f"Calling tool '{tool_call_name}' (args not serializable)")
                        for _chunk in conv.start_tool_call(tool_call_id, tool_call_name, tool_call_args, msg_id):
                            yield _chunk

                        try:
                            tool_call_res = await session.call_tool(tool_call_name, tool_call_args)
                        except Exception as exc:
                            logger.error(f"Tool call '{tool_call_name}' failed: {exc}")
                            err_text = f"Error calling tool {tool_call_name}: {exc}"
                            for _chunk in conv.end_tool_call(tool_call_id, tool_call_name, msg_id, err_text):
                                yield _chunk
                            lc_msgs.append(ToolMessage(content=err_text, tool_call_id=tool_call_id))
                            needs_synthesis = True
                            continue

                        logger.info(f"Tool call completed '{tool_call_name}'")
                        parsed, tool_results_str = _parse_tool_result(tool_call_res)
                        search_results = _as_search_results(parsed)

                        if search_results is not None and search_results.hits:
                            # Auto rerank search results with LLM
                            rerank_id = f"rerank_{tool_call_id}"
                            for _chunk in conv.start_tool_call(rerank_id, "rerank_results", tool_call_args, msg_id):
                                yield _chunk
                            ranked = await rerank_search_results(
                                search_input.model, [langfuse_handler], lc_msgs, search_results, token_usage
                            )
                            ranked_dump = ranked.model_dump(by_alias=True)
                            for _chunk in conv.end_tool_call(
                                rerank_id, "rerank_results", msg_id, json.dumps(ranked_dump)
                            ):
                                yield _chunk
                            lc_msgs.append(
                                ToolMessage(content=json.dumps(_truncate_hits(ranked_dump)), tool_call_id=tool_call_id)
                            )
                            rerank_summaries.append(ranked.summary)
                        else:
                            # Non-search tool (or empty results): end normally and feed the raw result back.
                            for _chunk in conv.end_tool_call(tool_call_id, tool_call_name, msg_id, tool_results_str):
                                yield _chunk
                            lc_history_str = (
                                json.dumps(_truncate_hits(parsed)) if parsed is not None else tool_results_str
                            )
                            lc_msgs.append(ToolMessage(content=lc_history_str or "(empty)", tool_call_id=tool_call_id))
                            needs_synthesis = True

                    # Stop loop if only reranked searches with summary, no need for extra LLM turn
                    if rerank_summaries and not needs_synthesis:
                        final_text += "\n\n".join(rerank_summaries)
                        break
            yield sse(RunFinishedEvent(thread_id=conv.thread_id, run_id=run_id, timestamp=get_timestamp()))
            langfuse.update_current_span(output={"text": final_text})
    except Exception as exc:
        logger.exception("Chat stream failed", extra={"endpoint": "/chat", "run_id": run_id})
        with contextlib.suppress(Exception):
            yield sse(RunErrorEvent(message=str(exc) or exc.__class__.__name__, timestamp=get_timestamp()))
    finally:
        if user is not None:
            new_items_start = max(0, initial_items_count - 1)
            new_items = conv.items[new_items_start:]
            if new_items:
                store_messages(
                    user=user,
                    thread_id=conv.thread_id,
                    items=new_items,
                )

        last_user_msg = ""
        for item in reversed(search_input.items):
            if isinstance(item, MessageItem) and item.role == "user":
                last_user_msg = "\n".join(part.text for part in item.content if part.type == "text")
                break
        logger.info(
            f'Completed query "{last_user_msg}" · {time.monotonic() - t0:.2f}s · {token_usage.model_dump()}',
            extra={
                "endpoint": "/chat",
                "model": used_model,
                "duration": time.monotonic() - t0,
                "token_usage": token_usage.model_dump(),
                "query": last_user_msg,
                "response": final_text,
            },
        )
        if final_text:
            file_logger.info(
                json.dumps(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "model": used_model,
                        "duration": time.monotonic() - t0,
                        "token_usage": token_usage.model_dump(),
                        "input": last_user_msg,
                        "response": final_text,
                    }
                )
            )


async def rerank_search_results(
    model: str,
    callbacks: Callbacks,
    chat_messages: list[AnyMessage],
    search_results: SearchResults,
    token_usage: TokenUsageMetadata,
) -> SummarizedSearchResponse:
    """Rerank search results using LLM with structured output.

    Args:
        model: The LLM model (provider/name) to use for reranking
        callbacks: LangChain callbacks (e.g. Langfuse) passed to the model
        chat_messages: Original chat messages for context
        search_results: Search results to rerank

    Returns:
        SummarizedSearchResponse with reranked hits and summary
    """
    # Use the latest user turn as the query
    last_user = next((m for m in reversed(chat_messages) if isinstance(m, HumanMessage)), None)
    query = last_user.content if last_user and isinstance(last_user.content, str) else ""
    formatted_context = f"Found {search_results.total_found} results relevant to the query '{query}':\n\n"
    for i, hit in enumerate(search_results.hits[: settings.search_results_count]):
        formatted_context += f"{i + 1}:\n"
        formatted_context += f"   {' | '.join([title.title for title in hit.source.titles])}\n"
        if hit.source.dates:
            formatted_context += (
                f"   Dates: {' | '.join([f'{date.date_type}: {date.date}' for date in hit.source.dates])}\n"
            )
        if hit.source.creators:
            authors = ", ".join([creator.creator_name for creator in hit.source.creators if creator.creator_name])
            if len(authors) > 200:
                authors = authors[:200].rstrip() + "..."
            formatted_context += f"   Authors: {authors}\n"
        if hit.source.subjects:
            formatted_context += f"   Keywords: {', '.join([subj.subject for subj in hit.source.subjects])}\n"
        desc = hit.description or ""
        if len(desc) > 800:
            desc = desc[:800].rstrip() + "..."
        formatted_context += f"   Description: {desc}\n\n"

    # Only pass plain user/assistant text turns; AIMessages with tool_calls would leave a
    # dangling tool-call turn (no matching ToolMessage yet) and cause the provider to drop
    # the structured-output tool call. Also strip system messages so the rerank prompt is first.
    rerank_context: list[AnyMessage] = [
        m for m in chat_messages if isinstance(m, HumanMessage) or (isinstance(m, AIMessage) and not m.tool_calls)
    ]
    rerank_msgs: list[AnyMessage] = [
        get_system_prompt(RERANK_PROMPT),
        *rerank_context,
        HumanMessage(content=formatted_context),
    ]
    try:
        # Call LLM with structured output for reranking; rate-limit falls back to the fallback model.
        llm_structured_rerank, _ = load_chat_model_with_fallback(
            model,
            lambda m: m.with_structured_output(RerankingOutput, method="json_schema", include_raw=True),
            callbacks=callbacks,
        )
        resp = await llm_structured_rerank.ainvoke(rerank_msgs)
        # logger.info(f"Reranking with context:\n{resp}")
        rerank_resp = RerankingOutputResponse.model_validate(resp)
        token_usage += LangChainResponseMetadata.model_validate(rerank_resp.raw.response_metadata).token_usage
        # if rerank_resp.parsed is None:
        #     raise ValueError(f"LLM returned no structured rerank output: {rerank_resp.parsing_error}")

        # Only keep the hits that were sent for reranking
        reranked_hits = search_results.hits[: settings.search_results_count]

        # Map LLM scores back by 1-based index (robust: small integers, unlike opaque ids the LLM
        # mistypes/omits). For any hit the LLM failed to score, fall back to its OpenSearch relevance
        # score instead of 0.0 - a missed near-duplicate must NOT crater to the bottom.
        score_by_index = {h.index: h.score for h in rerank_resp.parsed.hits}
        for i, hit in enumerate(reranked_hits):
            hit.score = score_by_index.get(i + 1, hit.opensearch_score)

        # Sort hits by score in descending order
        reranked_hits.sort(key=lambda h: h.score if h.score is not None else 0.0, reverse=True)
        # await get_relevant_tools(reranked_hits)
        return SummarizedSearchResponse(summary=rerank_resp.parsed.summary, hits=reranked_hits)
    except Exception as e:
        logger.error(f"Reranking failed: {e}")
        # Fallback: return results as-is without reranking
        return SummarizedSearchResponse(
            summary=f"Found {search_results.total_found} relevant datasets.",
            hits=search_results.hits,
        )


@app.get("/conversations")
async def list_conversations(user: UserInfo = Depends(require_auth)) -> list[ConversationSummary]:
    """Return a summary list of all conversations for the authenticated user, newest first."""
    # logger.info(f"User {user.preferred_username or user.sub} listed conversations")
    return get_conversations(user.sub)


@app.get("/conversation/{thread_id}")
async def get_conversation_endpoint(thread_id: str, user: UserInfo = Depends(require_auth)) -> ConversationDetail:
    """Return the full message history for a single conversation thread.

    Messages are returned in chronological order.
    Requires authentication and ownership of the thread.
    """
    detail = get_conversation(user.sub, thread_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return detail


@app.delete("/conversations")
async def delete_conversations_endpoint(
    thread_ids: list[str] = Body(...), user: UserInfo = Depends(require_auth)
) -> None:
    """Delete one or more conversations by their thread IDs. Only deletes conversations owned by the authenticated user."""
    delete_conversations(user.sub, thread_ids)


@app.get("/stats")
async def get_stats() -> DbStats:
    """Return pre-computed stats about the harvested records in datasetdb.

    Includes the dataset count per repository and the most popular subjects per repository.
    Stats are generated offline by `POSTGRES_DB=datasetdb uv run scripts/compute_stats.py`
    """
    stats = load_stats()
    if stats is None:
        raise HTTPException(status_code=404, detail="Stats not available")
    return stats


@app.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    """Redirect root path to the Swagger UI /docs."""
    return RedirectResponse(url="/docs")


app.include_router(auth_router)
app.include_router(vault_router)


# # NOTE: deprecated -Serve website built using vite
# app.mount(
#     "/assets",
#     StaticFiles(directory="src/data_commons_search/webapp/assets"),
#     name="static",
# )

# WEBAPP_HTML_PATH = "src/data_commons_search/webapp/index.html"

# @app.get("/")
# async def ui_handler(request: Request) -> FileResponse:
#     """Serve the chat UI HTML file directly."""
#     return FileResponse(WEBAPP_HTML_PATH)


# @app.get("/search")
# async def search_handler() -> FileResponse:
#     """Serve the chat UI HTML file for root path."""
#     return FileResponse(WEBAPP_HTML_PATH)


# @app.exception_handler(404)
# async def custom_404_handler(request: Request, exc: HTTPException) -> FileResponse:
#     """Handle 404 errors on the frontend."""
#     return FileResponse(WEBAPP_HTML_PATH)


# In OpenSearch and Filemetrix: https://doi.org/10.17026/DANS-2B8-ZGY2
# Data to Monitor Soil Aggregate Breakdown
# Data on fair evaluation

# NOTE: commented out for now as this is done directly from the frontend when a user show interest for a dataset (e.g. clicks on it)

# # https://confluence.egi.eu/display/EOSCDATACOMMONS/API+Definitions+and+Implementation+Guidelines
# # https://dev.matchmaker.eosc-data-commons.eu/search?q=search for data about Cognitive load in cyclists while navigating in traffic&model=cesnet%2Fqwen3-coder
# # curl -X POST http://localhost:8001/chat -H "Content-Type: application/json" -H "Authorization: SECRET_KEY" -d '{"messages": [{"role": "user", "content": "Datasets about representation of dogs in medieval time"}], "model": "cesnet/qwen3-coder", "stream": true}'
# # curl -X POST http://localhost:8001/chat -H "Content-Type: application/json" -H "Authorization: SECRET_KEY" -d '{"messages": [{"role": "user", "content": "search for data about Harelbeke Evolis"}], "model": "cesnet/qwen3-coder", "stream": true}'
# # curl -X POST http://localhost:8001/chat -H "Content-Type: application/json" -H "Authorization: SECRET_KEY" -d '{"messages": [{"role": "user", "content": "search for data about Cognitive load in cyclists while navigating in traffic"}], "model": "cesnet/qwen3-coder", "stream": true}'
# async def get_relevant_tools(search_hits: list[SearchHit]) -> None:
#     """Fetch file extensions and relevant tools from the FileMetrix API in parallel for each hit's DOI,
#     and update hits in-place.

#     Args:
#         search_results: The OpenSearch results to enhance with file extensions and relevant tools.
#     """

#     async def fetch_extensions(client: httpx.AsyncClient, doi: str) -> FileMetrixExtensionsResponse | None:
#         """Fetch extensions for a single DOI."""
#         try:
#             encoded = quote(doi, safe="")
#             resp = await client.get(
#                 f"{settings.filemetrix_api}/extensions/{encoded}",
#                 headers={"accept": "application/json"},
#             )
#             if resp.status_code == 200:
#                 return FileMetrixExtensionsResponse.model_validate(resp.json())
#             logger.warning(f"FileMetrix returned {resp.status_code} for DOI {doi}")
#         except Exception as e:
#             logger.warning(f"FileMetrix fetch error for {doi}: {e}")
#         return None

#     async def fetch_tools_for_extension(client: httpx.AsyncClient, extension: str) -> list[dict[str, str]] | None:
#         """Fetch relevant tools for a file extension from the tool registry."""
#         try:
#             resp = await client.get(
#                 f"{settings.tool_registry_api}/input/{extension}",
#                 headers={"accept": "application/json"},
#             )
#             if resp.status_code == 200:
#                 return resp.json()
#             logger.warning(f"Tool registry returned {resp.status_code} for extension {extension}")
#         except Exception as e:
#             logger.warning(f"Tool registry fetch error for {extension}: {e}")
#         return None

#     # Extract DOI from hit and create fetch task
#     async def process_hit(client: httpx.AsyncClient, hit: SearchHit) -> None:
#         """Extract DOI from hit and fetch/apply extensions and relevant tools."""
#         doi = None
#         try:
#             if hit.id.startswith("http"):
#                 parsed = urlparse(hit.id)
#                 if "doi.org" in parsed.netloc:
#                     doi = unquote(parsed.path.lstrip("/"))
#             else:
#                 doi = hit.id
#         except Exception:
#             return
#         if not doi:
#             return

#         # Fetch file extensions
#         fm = await fetch_extensions(client, doi)
#         if fm:
#             hit.file_extensions = fm.extensions
#             logger.info(f"📁 https://doi.org/{doi} -> extensions: {fm.extensions}")

#             # Fetch relevant tools for each extension
#             all_tools = []
#             for ext in fm.extensions:
#                 tools_data = await fetch_tools_for_extension(client, ext)
#                 if tools_data:
#                     try:
#                         for tool_dict in tools_data:
#                             tool = ToolRegistryTool.model_validate(tool_dict)
#                             all_tools.append(tool)
#                             logger.info(f"🔧 {ext} -> tool: {tool.tool_label}")
#                     except Exception as e:
#                         logger.warning(f"Error parsing tool data for {ext}: {e}")

#             # Remove duplicates by tool_uri while preserving order
#             seen = set()
#             unique_tools = []
#             for tool in all_tools:
#                 if tool.tool_uri not in seen:
#                     seen.add(tool.tool_uri)
#                     unique_tools.append(tool)

#             hit.relevant_tools = unique_tools

#     async with httpx.AsyncClient(timeout=10.0) as client:
#         await asyncio.gather(*(process_hit(client, hit) for hit in search_hits))
