"""HTTP API to deploy the EOSC Data Commons search agent."""

import contextlib
import json
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Generator
from datetime import datetime, timezone
from typing import Any

from ag_ui.core import (
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
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from langchain.chat_models import BaseChatModel
from langchain.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langfuse import Langfuse, propagate_attributes
from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler
from mcp.types import TextContent

from data_commons_search.auth import (
    UserInfo,
    apply_pending_auth_cookies,
    optional_auth,
    require_auth,
)
from data_commons_search.auth import (
    router as auth_router,
)
from data_commons_search.config import settings
from data_commons_search.db import (
    generate_unique_thread_id,
    get_conversation,
    get_conversations,
    init_postgres_storage,
    store_messages,
)
from data_commons_search.logging import BLUE, BOLD, RESET, YELLOW
from data_commons_search.mcp_server import mcp
from data_commons_search.models import (
    AgentInput,
    ConversationDetail,
    ConversationSummary,
    LangChainResponseMetadata,
    MessageItem,
    OpenSearchResults,
    RankedSearchResponse,
    RerankingOutput,
    RerankingOutputResponse,
    TextPart,
    TokenUsageMetadata,
    ToolCallItem,
    ToolResultItem,
)
from data_commons_search.prompts import RERANK_PROMPT, SUMMARIZE_PROMPT, TOOL_CALL_PROMPT
from data_commons_search.rate_limit import RateLimiter
from data_commons_search.utils import (
    file_logger,
    get_system_prompt,
    load_chat_model,
    logger,
    sse,
)

rate_limiter = RateLimiter(settings.redis_url)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan that initializes the MCP session manager."""
    init_postgres_storage()
    await rate_limiter.init()
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        await rate_limiter.aclose()


app = FastAPI(
    title="EOSC Data Commons Search API",
    description="A server for the [EOSC Data Commons project](https://eosc.eu/horizon-europe-projects/eosc-data-commons/) MatchMaker service, providing natural language search over open-access datasets. It exposes an HTTP POST endpoint and supports the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) to help users discover datasets and tools via a Large Language Model-assisted search.",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/mcp", mcp.streamable_http_app(), name="mcp")


if settings.cors_enabled:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

mcp_client = MultiServerMCPClient(
    {
        "data-commons-search": {
            "url": f"{settings.server_url}/mcp",
            "transport": "streamable_http",
        }
    }
)

# Initialize Langfuse client with host from settings (keys still come from env vars)
langfuse = Langfuse(
    host=settings.langfuse_base_url, public_key=settings.langfuse_public_key, secret_key=settings.langfuse_secret_key
)

logger.info(f"""💬 {BOLD}{BLUE}Search UI{RESET} started on {BOLD}{YELLOW}{settings.server_url}{RESET}
⚡️ Streamable HTTP MCP server started on {BOLD}{settings.server_url}/mcp{RESET}
🔓 Login on {BOLD}{settings.server_url}/auth/login{RESET}
🔎 Using OpenSearch service on {BOLD}{settings.opensearch_url}{RESET}""")


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
    if user is not None:
        if search_input.thread_id is None:
            search_input.thread_id = generate_unique_thread_id(user.sub)
    elif search_input.thread_id is None:
        search_input.thread_id = uuid.uuid4().hex

    response = StreamingResponse(
        stream_chat_response(search_input, user),
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


async def stream_chat_response(search_input: AgentInput, user: UserInfo | None = None) -> AsyncGenerator[str, None]:
    """Stream the chat response with tool calls, reranking, and results."""
    run_id = str(uuid.uuid4())
    token_usage = TokenUsageMetadata()

    conv = ConversationBuilder(search_input, user)
    # Track how many items were sent by the client so we only store new items
    initial_items_count = len(conv.items)

    yield sse(RunStartedEvent(thread_id=conv.thread_id, run_id=run_id, timestamp=get_timestamp()))

    final_response: RankedSearchResponse | None = None
    try:
        # Wrap the entire workflow in a single Langfuse trace
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
            # Initialize Langfuse callback handler inside the context so it inherits the current trace
            langfuse_handler = LangfuseCallbackHandler()

            # Get tools from the MCP client
            tools = await mcp_client.get_tools()

            # Get model with tools for the initial query
            llm = load_chat_model(search_input.model, callbacks=[langfuse_handler])
            llm_with_tools = llm.bind_tools(tools)

            # Step 1: Call LLM to get tool calls
            lc_msgs = conv.to_langchain()
            # TODO: move the system prompt inside `to_langchain()`?
            # TODO: just call conv.to_langchain() for most cases to ask feedback from LLM
            llm_start = get_timestamp()
            tc_llm_resp = llm_with_tools.invoke([get_system_prompt(TOOL_CALL_PROMPT), *lc_msgs])
            token_usage += LangChainResponseMetadata.model_validate(tc_llm_resp.response_metadata).token_usage

            if tc_llm_resp.content and isinstance(tc_llm_resp.content, str):
                # If tc_llm_resp has text send it as a TextMessage content alongside tool calls
                for _chunk in conv.add_msg(tc_llm_resp.content, start_time=llm_start):
                    yield _chunk

            # Step 2: Execute each tool and collect search results and textual outputs
            search_results = OpenSearchResults(total_found=0, hits=[])
            tool_text_outputs: list[str] = []
            async with mcp_client.session("data-commons-search") as session:
                for tool_call in tc_llm_resp.tool_calls:
                    tool_call_id = str(tool_call.get("id") or f"call_{uuid.uuid4().hex}")
                    tool_call_name = str(tool_call["name"])
                    tool_call_args = dict(tool_call["args"])
                    for _chunk in conv.start_tool_call(tool_call_id, tool_call_name, tool_call_args, conv.msg_id):
                        yield _chunk
                    tool_call_res = await session.call_tool(tool_call_name, tool_call_args)

                    if tool_call_res.structuredContent:
                        # Handle structured content, try to parse as `OpenSearchResults`
                        tool_results_str = json.dumps(tool_call_res.structuredContent)
                        try:
                            tool_results = OpenSearchResults(**tool_call_res.structuredContent)
                            search_results.hits.extend(tool_results.hits)
                            search_results.total_found += tool_results.total_found
                        except Exception as exc:
                            logger.warning("Could not parse structured content as OpenSearchResults: %s", exc)
                        for _chunk in conv.end_tool_call(tool_call_id, tool_call_name, conv.msg_id, tool_results_str):
                            yield _chunk
                    elif tool_call_res.content:
                        # Handle if text content is sent back; collect all text parts then emit a single result
                        tool_text = ""
                        for resp_content in tool_call_res.content:
                            if isinstance(resp_content, TextContent) and resp_content.text:
                                tool_text_outputs.append(resp_content.text)
                                tool_text += resp_content.text
                        for _chunk in conv.end_tool_call(tool_call_id, tool_call_name, conv.msg_id, tool_text):
                            yield _chunk
                    else:
                        yield sse(ToolCallEndEvent(tool_call_id=tool_call_id, timestamp=get_timestamp()))

            # TODO: improve this, just send back the conversation without asking summary?
            # Handle if there were tool calls output, but no search results: ask the LLM to summarize tools outputs
            if tc_llm_resp.tool_calls and search_results.total_found == 0 and tool_text_outputs:
                summary_msgs: list[AnyMessage] = [
                    get_system_prompt(SUMMARIZE_PROMPT),
                    *lc_msgs,
                    HumanMessage(
                        content=(
                            "The following tool outputs were produced when handling the user's query:\n\n"
                            + "\n\n---\n\n".join(tool_text_outputs)
                            + "\n\nPlease provide a concise summary for the user explaining what the tools returned and any recommendation or next steps."
                        )
                    ),
                ]

                try:
                    summary_start = get_timestamp()
                    summary_resp = llm.invoke(summary_msgs)
                    token_usage += LangChainResponseMetadata.model_validate(summary_resp.response_metadata).token_usage
                    summary_content = str(summary_resp.content)
                    for _chunk in conv.add_msg(summary_content, start_time=summary_start):
                        yield _chunk
                    langfuse.update_current_span(output={"summary": summary_content})
                    return
                except Exception as e:
                    logger.error(f"Fallback summarization failed: {e}")

            # Step 3: If no results found or no tool calls, handle early exit
            if not tc_llm_resp.tool_calls or search_results.total_found == 0:
                for _chunk in conv.add_msg("No results found"):
                    yield _chunk
                yield sse(RunFinishedEvent(thread_id=conv.thread_id, run_id=run_id, timestamp=get_timestamp()))
                langfuse.update_current_span(output={"message": "No results found"})
                return

            # print(json.dumps(search_results.model_dump(), indent=2))

            # Step 4: Rerank search results using LLM with structured output
            rerank_tc_id = "rerank_results"
            for _chunk in conv.start_tool_call(
                rerank_tc_id, "rerank_results", {"top_k": settings.reranking_results_count}, conv.msg_id
            ):
                yield _chunk
            final_response = await rerank_search_results(
                llm,
                lc_msgs,
                search_results,
                token_usage,
            )
            for _chunk in conv.end_tool_call(
                rerank_tc_id, "rerank_results", conv.msg_id, final_response.model_dump_json(by_alias=True)
            ):
                yield _chunk
            yield sse(RunFinishedEvent(thread_id=conv.thread_id, run_id=run_id, timestamp=get_timestamp()))

            # Update trace with final output
            langfuse.update_current_span(output=final_response.model_dump())
    finally:
        # Store only the new items from this turn: the current user message
        # (last item in the client-provided history) + server-generated items.
        # This avoids re-storing the full client-provided history on every turn.
        if user is not None:
            # print(conv.items[0])
            new_items_start = max(0, initial_items_count - 1)
            new_items = conv.items[new_items_start:]
            if new_items:
                store_messages(
                    user=user,
                    thread_id=conv.thread_id,
                    items=new_items,
                )

        # Log the user last msg and token usage for the run
        last_user_msg = ""
        for item in reversed(search_input.items):
            if isinstance(item, MessageItem) and item.role == "user":
                last_user_msg = "\n".join(part.text for part in item.content if part.type == "text")
                break
        logger.info(f'/chat "{last_user_msg}" | {token_usage.model_dump()}')
        if final_response is not None:
            # print(search_input.model_dump())
            file_logger.info(
                json.dumps(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "token_usage": token_usage.model_dump(),
                        # "input": search_input.model_dump(), # TODO: we need to handle datetimes in the items
                        "input": last_user_msg,
                        "response": final_response.model_dump(),
                    }
                )
            )


async def rerank_search_results(
    llm: BaseChatModel,
    chat_messages: list[AnyMessage],
    search_results: OpenSearchResults,
    token_usage: TokenUsageMetadata,
) -> RankedSearchResponse:
    """Rerank search results using LLM with structured output.

    Args:
        model: The LLM model to use for reranking
        chat_messages: Original chat messages for context
        search_results: Search results to rerank

    Returns:
        RankedSearchResponse with reranked hits and summary
    """
    # Format the context for the LLM
    last_msg = chat_messages[-1] if chat_messages else None
    last_msg_content = last_msg.content if last_msg and isinstance(last_msg.content, str) else ""
    formatted_context = f"Found {search_results.total_found} datasets relevant to the query '{last_msg_content}':\n\n"
    for i, hit in enumerate(search_results.hits[: settings.reranking_results_count]):
        formatted_context += f"{i + 1}. **{hit.id}**\n"
        formatted_context += f"   {' | '.join([title.title for title in hit.source.titles])}\n"
        if hit.source.dates:
            formatted_context += (
                f"   Dates: {' | '.join([f'{date.date_type}: {date.date}' for date in hit.source.dates])}\n"
            )
        if hit.source.creators:
            formatted_context += f"   Authors: {', '.join([creator.creator_name for creator in hit.source.creators if creator.creator_name])}\n"
        if hit.source.subjects:
            formatted_context += f"   Keywords: {', '.join([subj.subject for subj in hit.source.subjects])}\n"
        formatted_context += f"   Description: {hit.description}\n\n"

    rerank_msgs: list[AnyMessage] = [
        get_system_prompt(RERANK_PROMPT),
        *chat_messages,
        HumanMessage(content=formatted_context),
    ]
    try:
        # Call LLM with structured output for reranking
        llm_structured_rerank = llm.with_structured_output(RerankingOutput, method="function_calling", include_raw=True)
        rerank_resp = RerankingOutputResponse.model_validate(llm_structured_rerank.invoke(rerank_msgs))
        token_usage += LangChainResponseMetadata.model_validate(rerank_resp.raw.response_metadata).token_usage

        # Only keep the hits that were sent for reranking
        reranked_hits = search_results.hits[: settings.reranking_results_count]

        # Add scores to the reranked datasets
        score_lookup = {hit.url: hit.score for hit in rerank_resp.parsed.hits}
        # print(f"Rerank response: {score_lookup}")
        for hit in reranked_hits:
            hit.score = score_lookup.get(hit.id, 0.0)

        # Sort hits by score in descending order
        reranked_hits.sort(key=lambda h: h.score or 0.0, reverse=True)
        # await get_relevant_tools(reranked_hits)
        return RankedSearchResponse(summary=rerank_resp.parsed.summary, hits=reranked_hits)
    except Exception as e:
        logger.error(f"Reranking failed: {e}")
        # Fallback: return results as-is without reranking
        return RankedSearchResponse(
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


@app.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    """Redirect root path to the Swagger UI /docs."""
    return RedirectResponse(url="/docs")


app.include_router(auth_router)


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
# # https://dev.matchmaker.eosc-data-commons.eu/search?q=search for data about Cognitive load in cyclists while navigating in traffic&model=einfracz%2Fqwen3-coder
# # curl -X POST http://localhost:8001/chat -H "Content-Type: application/json" -H "Authorization: SECRET_KEY" -d '{"messages": [{"role": "user", "content": "Datasets about representation of dogs in medieval time"}], "model": "einfracz/qwen3-coder", "stream": true}'
# # curl -X POST http://localhost:8001/chat -H "Content-Type: application/json" -H "Authorization: SECRET_KEY" -d '{"messages": [{"role": "user", "content": "search for data about Harelbeke Evolis"}], "model": "einfracz/qwen3-coder", "stream": true}'
# # curl -X POST http://localhost:8001/chat -H "Content-Type: application/json" -H "Authorization: SECRET_KEY" -d '{"messages": [{"role": "user", "content": "search for data about Cognitive load in cyclists while navigating in traffic"}], "model": "einfracz/qwen3-coder", "stream": true}'
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
