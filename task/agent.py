import json
from typing import Any

from aidial_client import AsyncDial
from aidial_sdk.chat_completion import Role, Choice, Request, Message, Stage

from task.coordination.gpa import GPAGateway
from task.coordination.ums_agent import UMSAgentGateway
from task.logging_config import get_logger
from task.models import CoordinationRequest, AgentName
from task.prompts import COORDINATION_REQUEST_SYSTEM_PROMPT, FINAL_RESPONSE_SYSTEM_PROMPT
from task.stage_util import StageProcessor

logger = get_logger(__name__)

class MASCoordinator:

    def __init__(self, endpoint: str, deployment_name: str, ums_agent_endpoint: str):
        self.endpoint = endpoint
        self.deployment_name = deployment_name
        self.ums_agent_endpoint = ums_agent_endpoint

    async def handle_request(self, choice: Choice, request: Request) -> Message:
        async_dial = AsyncDial(api_version='2025-01-01-preview', base_url=self.endpoint, api_key=request.api_key)
        coordination_request = await self.__prepare_coordination_request(async_dial, request)
        stage = StageProcessor.open_stage(choice, name="Coordination Request")
        stage.append_content(coordination_request.model_dump_json())
        stage.close()
        stage = StageProcessor.open_stage(choice, name=f"Call {coordination_request.agent_name} agent")
        message = await self.__handle_coordination_request(coordination_request, choice, stage, request)
        stage.close()
        final_response = await self.__final_response(async_dial, choice, request, message)
        return final_response


    async def __prepare_coordination_request(
            self, client: AsyncDial, request: Request
    ) -> CoordinationRequest:
        messages = self.__prepare_messages(
            request=request,
            system_prompt=COORDINATION_REQUEST_SYSTEM_PROMPT
        )

        response = await client.chat.completions.create(
            deployment_name=self.deployment_name,
            messages=messages,
            extra_body={
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "coordination_request",
                        "schema": CoordinationRequest.model_json_schema()
                    }
                }
            }
        )

        content = response.choices[0].message.content
        data = json.loads(content)

        return CoordinationRequest.model_validate(data)

    def __prepare_messages(
            self, request: Request, system_prompt: str
    ) -> list[dict[str, Any]]:

        messages: list[dict[str, Any]] = [{
            "role": Role.SYSTEM,
            "content": system_prompt
        }]

        for msg in request.messages:
            if msg.role == Role.USER and msg.custom_content:
                messages.append({
                    "role": Role.USER,
                    "content": msg.content
                })
            else:
                messages.append(msg.dict(exclude_none=True))

        return messages

    async def __handle_coordination_request(
            self,
            coordination_request: CoordinationRequest,
            choice: Choice,
            stage: Stage,
            request: Request
    ) -> Message:

        if coordination_request.agent_name == AgentName.GPA:
            agent = GPAGateway(self.endpoint)

        elif coordination_request.agent_name == AgentName.UMS:
            agent = UMSAgentGateway(self.ums_agent_endpoint)

        else:
            raise ValueError(
                f"Unsupported agent: {coordination_request.agent_name}"
            )

        agent_message = await agent.response(
            choice=choice,
            request=request,
            additional_instructions=coordination_request.additional_instructions,
            stage=stage
        )

        return agent_message

    async def __final_response(
            self,
            client: AsyncDial,
            choice: Choice,
            request: Request,
            agent_message: Message
    ) -> Message:

        messages = self.__prepare_messages(
            request=request,
            system_prompt=FINAL_RESPONSE_SYSTEM_PROMPT
        )

        augmented_user_prompt = (
            f"Context from agent:\n{agent_message.content}\n\n"
            f"User request:\n{messages[-1]['content']}"
        )

        messages[-1]["content"] = augmented_user_prompt

        stream = await client.chat.completions.create(
            deployment_name=self.deployment_name,
            messages=messages,
            stream=True
        )

        final_message = Message(role=Role.ASSISTANT, content="")

        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                final_message.content += delta
                choice.append_content(delta)

        return final_message
