from datetime import datetime
from typing import Dict, List, Tuple

import discord
import orjson
from openai.embeddings_utils import cosine_similarity
from pydantic import BaseModel

from .common.utils import num_tokens_from_string, token_pagify

MODELS = ["gpt-3.5-turbo", "gpt-4", "gpt-4-32k"]
READ_EXTENSIONS = [".txt", ".py", ".json", ".xml", ".html", ".ini", ".css"]


class Embedding(BaseModel):
    text: str
    embedding: List[float]


class GuildSettings(BaseModel):
    system_prompt: str = "You are a helpful discord assistant named {botname}"
    prompt: str = "Current time: {timestamp}\nDiscord server you are chatting in: {server}"
    embeddings: Dict[str, Embedding] = {}
    top_n: int = 3
    min_relatedness: float = 0.75
    dynamic_embedding: bool = True
    channel_id: int = 0
    api_key: str = ""
    endswith_questionmark: bool = False
    max_retention: int = 0
    max_retention_time: int = 1800
    max_tokens: int = 4000
    min_length: int = 7
    mention: bool = False
    enabled: bool = True
    model: str = "gpt-3.5-turbo"

    def get_related_embeddings(self, query_embedding: List[float]) -> List[Tuple[str, float]]:
        if not self.top_n or not query_embedding or not self.embeddings:
            return []
        strings_and_relatedness = [
            (i.text, cosine_similarity(query_embedding, i.embedding))
            for i in self.embeddings.values()
        ]
        strings_and_relatedness = [
            i for i in strings_and_relatedness if i[1] >= self.min_relatedness
        ]
        strings_and_relatedness.sort(key=lambda x: x[1], reverse=True)
        return strings_and_relatedness[: self.top_n]


class DB(BaseModel):
    configs: dict[int, GuildSettings] = {}

    class Config:
        json_loads = orjson.loads
        json_dumps = orjson.dumps

    def get_conf(self, guild: discord.Guild) -> GuildSettings:
        if guild.id in self.configs:
            return self.configs[guild.id]

        self.configs[guild.id] = GuildSettings()
        return self.configs[guild.id]


class Conversation(BaseModel):
    messages: list[dict[str, str]] = []
    last_updated: float = 0.0

    def user_token_count(self) -> int:
        messages = "".join(message["content"] for message in self.messages)
        if not self.messages:
            messages = ""
        return num_tokens_from_string(messages)

    def conversation_token_count(self, conf: GuildSettings) -> int:
        initial = conf.system_prompt + conf.prompt
        return num_tokens_from_string(initial) + self.user_token_count()

    def is_expired(self, conf: GuildSettings):
        if not conf.max_retention_time:
            return False
        return (datetime.now().timestamp() - self.last_updated) > conf.max_retention_time

    def cleanup(self, conf: GuildSettings):
        clear = [
            self.is_expired(conf),
            not conf.max_retention,
        ]
        if any(clear):
            self.messages.clear()
        elif conf.max_retention:
            self.messages = self.messages[-conf.max_retention :]


    def reset(self):
        self.last_updated = datetime.now().timestamp()
        self.messages.clear()

    def update_messages(self, message: str, role: str) -> None:
        """Update conversation cache

        Args:
            message (str): the message
            role (str): 'system', 'user' or 'assistant'
            name (str): the name of the bot or user
        """
        self.messages.append({"role": role, "content": message})
        self.last_updated = datetime.now().timestamp()

    def prepare_chat(
        self,
        conf: GuildSettings,
        system_prompt: str = "",
        initial_prompt: str = "",
        ) -> List[dict]:
        prepared = []
        if system_prompt:
            prepared.append({"role": "system", "content": system_prompt})
        if initial_prompt:
            prepared.append({"role": "user", "content": initial_prompt})
        prepared.extend(self.messages)
        # 4096 is max tokens for 3.5
        while self.conversation_token_count(conf) > conf.max_tokens * 0.95 and len(self.messages) > 1:
            self.messages.pop(0)
        if self.conversation_token_count(conf) > conf.max_tokens * 0.95:
            chunks = [
                p for p in token_pagify(self.messages[0]["content"], max_tokens=round(conf.max_tokens * 0.9))
                ]
            self.messages[0]["content"] = chunks[0]
        return prepared


class Conversations(BaseModel):
    """Temporary conversation cache"""

    conversations: dict[int, Conversation] = {}

    def get_conversation(self, member: discord.Member) -> Conversation:
        key = f"{member.id}{member.guild.id}"
        if key in self.conversations:
            return self.conversations[key]

        self.conversations[key] = Conversation()
        return self.conversations[key]
