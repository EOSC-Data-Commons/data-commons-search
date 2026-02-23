"""Utilities for the AI agent, e.g. load model."""

import logging
import os
import pathlib
from datetime import datetime

from langchain.chat_models import BaseChatModel, init_chat_model
from langchain.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, SecretStr

from data_commons_search.config import settings
from data_commons_search.models import AgentMsg

# Disable logger in your code with `logging.getLogger("data_commons_search").setLevel(logging.WARNING)`
logger = logging.getLogger("data_commons_search")
logger.setLevel(logging.INFO)
# handler = logging.StreamHandler()
# formatter = logging.Formatter("\x1b[90m%(asctime)s\x1b[0m [%(levelname)s] %(message)s")
# handler.setFormatter(formatter)
# logger.addHandler(handler)
# logger.propagate = False
# GREY = "\x1b[90m" RESET = "\x1b[0m"

# Log conversations to a file
file_logger = logging.getLogger("conversation_logger")
file_logger.setLevel(logging.INFO)
try:
    if not os.path.exists(settings.logs_filepath):
        pathlib.Path(settings.logs_filepath).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(settings.logs_filepath).touch()
    file_handler = logging.FileHandler(settings.logs_filepath)
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    file_logger.addHandler(file_handler)
    file_logger.propagate = False
except Exception:
    logger.warning(f"⚠️ Logs filepath {settings.logs_filepath} not writable.")

logging.getLogger("httpx").setLevel(logging.WARNING)

if settings.debug_enabled:
    logging.getLogger("uvicorn").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("opensearch").setLevel(logging.INFO)
    logging.getLogger("mcp").setLevel(logging.INFO)

# if not settings.debug_enabled:
#     logging.getLogger("uvicorn").setLevel(logging.WARNING)
#     logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
#     # logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
#     logging.getLogger("httpx").setLevel(logging.WARNING)
#     logging.getLogger("opensearch").setLevel(logging.WARNING)
#     logging.getLogger("mcp").setLevel(logging.WARNING)


def sse_event(data: BaseModel) -> str:
    """Format data as a Server-Sent Events (SSE) event string."""
    return f"data: {data.model_dump_json()}\n\n"


def get_system_prompt(prompt: str) -> SystemMessage:
    """Get the system prompt with current date."""
    return SystemMessage(prompt.format(current_date=datetime.now().strftime("%Y-%m-%d")))


def get_langchain_msgs(msgs: list[AgentMsg]) -> list[AnyMessage]:
    """Convert messages from ChatCompletionRequest to LangChain format."""
    new_msgs: list[AnyMessage] = []
    for msg in msgs:
        if msg.role == "human":
            new_msgs.append(HumanMessage(content=msg.content))
        elif msg.role == "ai":
            new_msgs.append(AIMessage(content=msg.content))
        elif msg.role == "system":
            new_msgs.append(SystemMessage(content=msg.content))
        elif msg.role == "tool":
            new_msgs.append(ToolMessage(content=msg.content, tool_call_id=getattr(msg, "tool_call_id", "")))
        else:
            new_msgs.append(HumanMessage(content=msg.content))
    return new_msgs


def load_chat_model(model: str) -> BaseChatModel:
    """Load a chat model from a fully specified name.

    Args:
        fully_specified_name (str): String in the format 'provider/model'.
    """
    provider, model_name = model.split("/", maxsplit=1)

    if provider == "einfracz":
        # https://chat.ai.e-infra.cz
        return ChatOpenAI(
            base_url="https://llm.ai.e-infra.cz/v1",
            model=model_name,
            api_key=SecretStr(settings.einfracz_api_key),
            max_completion_tokens=settings.llm_max_tokens,
        )

    if provider == "openrouter":
        # https://openrouter.ai/docs/community/lang-chain
        return ChatOpenAI(
            base_url="https://openrouter.ai/api/v1",
            model=model_name,
            api_key=SecretStr(settings.openrouter_api_key),
            max_completion_tokens=settings.llm_max_tokens,
            # default_headers={
            #     "HTTP-Referer": getenv("YOUR_SITE_URL"),
            #     "X-Title": getenv("YOUR_SITE_NAME"),
            # },
        )

    # if provider == "groq":
    #     # https://python.langchain.com/docs/integrations/chat/groq/
    #     from langchain_groq import ChatGroq

    #     return ChatGroq(
    #         model=model_name,
    #         max_tokens=configuration.max_tokens,
    #         temperature=configuration.temperature,
    #         timeout=None,
    #         max_retries=2,
    #     )
    # if provider == "together":
    #     # https://python.langchain.com/docs/integrations/chat/together/
    #     from langchain_together import ChatTogether
    #     return ChatTogether(
    #         model=model_name,
    #         max_tokens=configuration.max_tokens,
    #         temperature=configuration.temperature,
    #         timeout=None,
    #         max_retries=2,
    #     )
    # if provider == "hf":
    #     # https://python.langchain.com/docs/integrations/chat/huggingface/
    #     from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint
    #     return ChatHuggingFace(
    #         llm=HuggingFaceEndpoint(
    #             # repo_id="HuggingFaceH4/zephyr-7b-beta",
    #             repo_id=model_name,
    #             task="text-generation",
    #             max_new_tokens=configuration.max_tokens,
    #             do_sample=False,
    #             repetition_penalty=1.03,
    #         )
    #     )
    # if provider == "azure":
    #     # https://learn.microsoft.com/en-us/azure/ai-studio/how-to/develop/langchain
    #     from langchain_azure_ai.chat_models import AzureAIChatCompletionsModel
    #     return AzureAIChatCompletionsModel(
    #         endpoint=settings.azure_inference_endpoint,
    #         credential=settings.azure_inference_credential,
    #         model_name=model_name,
    #     )
    # if provider == "deepseek":
    #     # https://python.langchain.com/docs/integrations/chat/deepseek/
    #     from langchain_deepseek import ChatDeepSeek
    #     return ChatDeepSeek(
    #         model=model_name,
    #         temperature=configuration.temperature,
    #     )
    return init_chat_model(
        model_name,
        model_provider=provider,
        timeout=None,
        max_retries=2,
        # max_tokens=configuration.max_tokens,
        # temperature=configuration.temperature,
        # seed=configuration.seed,
        # reasoning={
        #     "effort": "low",  # 'low', 'medium', or 'high'
        #     "summary": "auto",  # 'detailed', 'auto', or None
        # },
    )


# def get_msg_text(msg: BaseMessage) -> str:
#     """Get the text content of a chat message."""
#     content = msg.content
#     if isinstance(content, str):
#         return content
#     elif isinstance(content, dict):
#         return content.get("text", "")
#     else:
#         txts = [c if isinstance(c, str) else (c.get("text") or "") for c in content]
#         return "".join(txts).strip()
