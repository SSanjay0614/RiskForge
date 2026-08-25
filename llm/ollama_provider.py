from langchain_ollama import ChatOllama

from config import (
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    TEMPERATURE
)


class OllamaProvider:

    def __init__(self):

        self.llm = ChatOllama(
            model=OLLAMA_MODEL,
            base_url=OLLAMA_BASE_URL,
            temperature=TEMPERATURE,
            num_predict=4096
        )

    def get_llm(self):
        return self.llm


ollama_provider = OllamaProvider()
