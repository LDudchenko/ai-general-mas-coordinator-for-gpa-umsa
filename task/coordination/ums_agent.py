import json
from typing import Optional

import httpx
from aidial_sdk.chat_completion import Role, Request, Message, Stage, Choice
from pydantic import StrictStr


_UMS_CONVERSATION_ID = "ums_conversation_id"


class UMSAgentGateway:

    def __init__(self, ums_agent_endpoint: str):
        self.ums_agent_endpoint = ums_agent_endpoint

    async def response(
            self,
            choice: Choice,
            stage: Stage,
            request: Request,
            additional_instructions: Optional[str]
    ) -> Message:

        conversation_id = self.__get_ums_conversation_id(request)

        if not conversation_id:
            conversation_id = await self.__create_ums_conversation()
            choice.set_state({_UMS_CONVERSATION_ID: conversation_id})

        last_message: Message = request.messages[-1]
        user_content = last_message.content

        if not isinstance(user_content, str):
            raise ValueError("UMS agent expects text user message")

        if additional_instructions:
            user_content = (
                f"{user_content}\n\n"
                f"Additional instructions:\n{additional_instructions}"
            )

        result_text = await self.__call_ums_agent(
            conversation_id=conversation_id,
            user_message=user_content,
            stage=stage
        )

        return Message(
            role=Role.ASSISTANT,
            content=StrictStr(result_text),
        )

    def __get_ums_conversation_id(self, request: Request) -> Optional[str]:
        for message in request.messages:
            if message.custom_content and isinstance(message.custom_content, dict):
                state = message.custom_content.get("state")
                if state and _UMS_CONVERSATION_ID in state:
                    return state[_UMS_CONVERSATION_ID]
        return None

    async def __create_ums_conversation(self) -> str:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.ums_agent_endpoint}/conversations",
                json={"title": "UMS Conversation"}
            )
            response.raise_for_status()
            data = response.json()
            return data["id"]

    async def __call_ums_agent(
            self,
            conversation_id: str,
            user_message: str,
            stage: Stage
    ) -> str:

        accumulated_text = ""

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                    "POST",
                    f"{self.ums_agent_endpoint}/chat/{conversation_id}",
                    json={
                        "message": {
                            "role": "user",
                            "content": user_message
                        },
                        "stream": True
                    }
            ) as response:
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data: ").strip()
                    if data == "[DONE]":
                        break
                    payload = json.loads(data)
                    if "choices" in payload:
                        delta = payload["choices"][0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            accumulated_text += content
                            stage.append_content(content)

        return accumulated_text
