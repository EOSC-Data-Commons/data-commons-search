import numpy as np
from typing import List
from langchain_openai import OpenAIEmbeddings
from data_commons_search.config import settings

class OnlineEmbedding:
    def __init__(self, model_name: str, api_key: str, base_url: str):
        self.client = OpenAIEmbeddings(api_key=api_key,
                                       base_url=base_url,
                                       model=model_name,
                                       check_embedding_ctx_length=False,
                                       dimensions=settings.embedding_dimensions)
    def embed(self, texts: List[str]) -> List[np.ndarray]:
        embeddings = []
        # call API
        response = self.client.embed_documents(texts)
        # return to numpy array
        return np.array(response)
