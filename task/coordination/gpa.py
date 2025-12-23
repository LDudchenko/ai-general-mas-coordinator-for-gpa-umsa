from copy import deepcopy
from typing import Optional, Any

from aidial_client import AsyncDial
from aidial_sdk.chat_completion import Role, Choice, Request, Message, CustomContent, Stage, Attachment
from pydantic import StrictStr

from task.stage_util import StageProcessor

_IS_GPA = "is_gpa"
_GPA_MESSAGES = "gpa_messages"


class GPAGateway:

    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    async def response(
            self,
            choice: Choice,
            stage: Stage,
            request: Request,
            additional_instructions: Optional[str]
    ) -> Message:
        async_dial = AsyncDial(api_version="2025-01-01-preview", base_url=self.endpoint, api_key=request.api_key)
        messages = self.__prepare_gpa_messages(request, additional_instructions)
        content = ""
        result_custom_content = CustomContent(attachments=[], state={})
        stages_map: dict[int, Stage] = {}
        chunks = await async_dial.chat.completions.create(deployment_name="general-purpose-agent", messages=messages,
                                                       stream=True, extra_headers={
                "x-conversation-id": request.headers.get("x-conversation-id")}, )
        async for chunk in chunks:
            delta = chunk.choices[0].delta

            if delta.content:
                print(delta.content, end="", flush=True)
                content += delta.content
                stage.append_content(delta.content)

            if delta.custom_content:

                if delta.custom_content.attachments:
                        result_custom_content.attachments.extend(delta.custom_content.attachments)

                if delta.custom_content.state:
                    result_custom_content.state = delta.custom_content.state

                custom_content_dict = delta.custom_content.dict(exclude_none=True)
                if "stages" in custom_content_dict:
                    for stg in custom_content_dict["stages"]:
                        idx = stg["index"]

                        if idx in stages_map:
                            mapped_stage = stages_map[idx]
                            if stg.get("content"):
                                mapped_stage.append_content(stg["content"])
                            if stg.get("attachments"):
                                for att in stg["attachments"]:
                                    mapped_stage.add_attachment(Attachment(**att))
                            if stg.get("status") == "completed":
                                StageProcessor.close_stage_safely(mapped_stage)
                        else:
                            mapped_stage = StageProcessor.open_stage(choice, name=stg.get("name"))
                            stages_map[idx] = mapped_stage


        for attachment in result_custom_content.attachments:
            choice.add_attachment(Attachment(**attachment.dict(exclude_none=True)))

        choice.state = {_IS_GPA: True, _GPA_MESSAGES: result_custom_content.state}

        return Message(role=Role.ASSISTANT, content=content)


    def __prepare_gpa_messages(self, request: Request, additional_instructions: Optional[str]) -> list[dict[str, Any]]:
        res_messages: list[dict[str, Any]] = []

        for idx in range(len(request.messages)):
            msg = request.messages[idx]

            if msg.role == Role.ASSISTANT and msg.custom_content and msg.custom_content.state:
                state = msg.custom_content.state

                if state.get(_IS_GPA):
                    res_messages.append(request.messages[idx - 1].dict(exclude_none=True))
                    restored = deepcopy(msg)
                    restored.custom_content.state = state.get(_GPA_MESSAGES, {})
                    res_messages.append(restored.dict(exclude_none=True))

        last_user_msg = request.messages[-1]
        custom_content = last_user_msg.custom_content
        if additional_instructions:
            last_msg = {
                "role": Role.USER,
                "content": additional_instructions,
                "custom_content": custom_content.dict(exclude_none=True) if custom_content else None,
            }
            res_messages.append(last_msg)
        else:
            res_messages.append(last_user_msg.dict(exclude_none=True))

        return res_messages

